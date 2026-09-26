#!/usr/bin/env python3

import asyncio
import importlib.util
import json
import os
import stat
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "slack_register.py"
sys.path.insert(0, str(MODULE_PATH.parent))
spec = importlib.util.spec_from_file_location("slack_register_tested", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def credentials(app="A-test"):
    return {
        "SLACK_APP_TOKEN": "xapp-test",
        "SLACK_BOT_TOKEN": "fake-bot-test",
        "SLACK_APP_ID": app,
        "BOT_USER_ID": "B-test",
        "OPERATOR_USER_ID": "U-operator",
    }


def registry(agents=None, tombstones=None):
    return {
        "version": 2,
        "machine_id": "machine-test",
        "agents": agents or {},
        "tombstones": tombstones or {},
    }


class SlackRegisterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "config"
        self.agents = self.root / "agents"
        self.pty = Path(self.temporary.name) / "pty"
        self.pty.mkdir(mode=0o700)
        self.patches = (
            mock.patch.object(module, "CONFIG_DIR", self.root),
            mock.patch.object(module, "AGENTS_DIR", self.agents),
            mock.patch.object(module, "REGISTRY_PATH", self.root / "registry.json"),
            mock.patch.object(module, "LOCK_PATH", self.root / "registry.lock"),
            mock.patch.object(module, "PTY_STATE_DIR", self.pty),
            mock.patch.object(module, "SLACK_CREDENTIALS", self.root / "credentials.json"),
            mock.patch.object(module.socket, "gethostname", return_value="machine-test"),
        )
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def write_registered(self, name="test-agent", app="A-test", session="session-test", **extra):
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.agents.mkdir(mode=0o700, exist_ok=True)
        values = credentials(app) | {
            "AGENT_KIND": "claude",
            "WORKDIR": "/work",
            "DELIVER_CHANNEL_MESSAGES": "false",
            "CLAUDE_CONFIG_DIR": "/profile",
            "CLAUDE_ARGS": "--model sonnet",
        } | extra
        module._write(self.agents / f"{name}.env", module._serialize(values))
        module._save(
            registry(
                {
                    name: {
                        "app_id": app,
                        "session_id": session,
                        "last_registered": 10,
                    }
                }
            )
        )

    def test_operator_user_ids_reads_every_line_of_operator_txt(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "operator.txt").write_text("U-one\nU-two\n\n")
            with mock.patch.object(module, "CONFIG_DIR", Path(directory)):
                self.assertEqual(module.operator_user_ids(), {"U-one", "U-two"})
            with mock.patch.object(module, "CONFIG_DIR", Path(directory) / "missing"):
                self.assertEqual(module.operator_user_ids(), set())

    def test_loaders_strictly_merge_private_env_and_registry(self):
        self.write_registered()
        loaded = module.load_agent_configs(self.agents)["test-agent"]
        self.assertEqual(loaded.session_id, "session-test")
        self.assertEqual(loaded.profile_dir, Path("/profile"))
        self.assertEqual(loaded.runtime_args, "--model sonnet")
        self.assertFalse(loaded.deliver_channel_messages)

        (self.agents / "test-agent.env").chmod(0o644)
        with self.assertRaises(module.RegisterError):
            module.load_agent_configs(self.agents)

    def test_registration_is_private_atomic(self):
        supplied = credentials()
        with (
            mock.patch.object(module, "_credentials", return_value=supplied) as provision,
            mock.patch.object(module, "_announce") as announce,
            mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "live-session"}),
        ):
            module._register(
                "test-agent",
                "claude",
                Path("/work"),
                Path("/profile"),
                "",
                "T-test",
                (),
            )
        provision.assert_called_once_with("test-agent", None, "T-test", (), mock.ANY)
        announce.assert_called_once_with("test-agent", mock.ANY)
        loaded = module.load_agent_configs(self.agents)["test-agent"]
        self.assertEqual(loaded.session_id, "live-session")
        self.assertEqual(stat.S_IMODE((self.root / "registry.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.agents / "test-agent.env").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.agents.stat().st_mode), 0o700)

    def test_new_name_for_same_session_retires_the_old_identity(self):
        self.write_registered(name="old-name", session="live-session")
        joined = []

        def bot_api(method, token, **fields):
            if method == "users.conversations":
                return {"channels": [{"id": "C-inherit"}]}
            if method == "conversations.invite":
                joined.append((fields["channel"], fields["users"]))
            return {}

        with (
            mock.patch.object(module, "_credentials", return_value=credentials("A-new")),
            mock.patch.object(module, "_api", side_effect=bot_api),
            mock.patch.object(module, "_announce"),
            mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "live-session"}),
        ):
            module._register(
                "new-name", "claude", Path("/work"), Path("/profile"),
                "", "T-test", (),
            )
        registry = module.load_registry(self.root / "registry.json")
        self.assertIn("new-name", registry["agents"])
        self.assertNotIn("old-name", registry["agents"])
        self.assertIn("old-name", registry["tombstones"])
        self.assertIsNone(registry["tombstones"]["old-name"]["session_id"])
        self.assertFalse((self.agents / "old-name.env").exists())
        self.assertTrue((self.agents / "new-name.env").exists())
        self.assertEqual(joined, [("C-inherit", "B-test")])

    def test_bot_channels_omits_none_cursor(self):
        self.write_registered(name="test")
        calls = []

        def fake_api(method, token=None, **fields):
            calls.append((method, fields))
            return {"channels": [{"id": "C-1"}], "response_metadata": {}}

        with mock.patch.object(module, "_api", side_effect=fake_api):
            channels = module._bot_channels(self.agents / "test.env")
        self.assertEqual(list(channels), ["C-1"])
        self.assertEqual(calls[0][1]["types"], "public_channel,private_channel")
        self.assertNotIn("cursor", calls[0][1])

    def test_valid_existing_name_joins_without_operator_credentials(self):
        self.write_registered()
        joined = []
        verified = []

        def bot_api(method, token, **fields):
            if method == "auth.test":
                return {"user_id": "B-test"}
            if method == "conversations.list":
                return {"channels": [{"id": "C-new", "name": "new-channel"}]}
            if method == "conversations.join":
                joined.append((token, fields["channel"]))
                return {"ok": True}
            if method == "conversations.info":
                verified.append((token, fields["channel"]))
                return {"channel": {"is_member": True}}
            raise AssertionError(f"unexpected Slack method: {method}")

        with (
            mock.patch.object(module, "_api", side_effect=bot_api),
            mock.patch.object(module, "_credentials") as provision,
            mock.patch.object(module, "_slack_credentials", side_effect=AssertionError("operator credential touched")),
            mock.patch.object(module, "_announce") as announce,
            mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "new-session"}),
        ):
            module._register(
                "test-agent", "claude", Path("/new-work"), Path("/new-profile"),
                "--fast", "T-test", ("new-channel",),
            )
        provision.assert_not_called()
        self.assertEqual(joined, [("fake-bot-test", "C-new")])
        self.assertEqual(verified, [("fake-bot-test", "C-new")])
        loaded = module.load_agent_configs(self.agents)["test-agent"]
        self.assertEqual(
            (loaded.workdir, loaded.profile_dir, loaded.runtime_args, loaded.session_id),
            (Path("/new-work"), Path("/new-profile"), "--fast", "new-session"),
        )
        self.assertEqual(
            (loaded.app_token, loaded.bot_token, loaded.app_id, loaded.bot_user_id, loaded.kind),
            ("xapp-test", "fake-bot-test", "A-test", "B-test", "claude"),
        )
        self.assertFalse(loaded.deliver_channel_messages)
        saved = module.load_registry(self.root / "registry.json")["agents"]["test-agent"]
        self.assertEqual(saved["session_id"], "new-session")
        self.assertGreater(saved["last_registered"], 10)
        announce.assert_called_once_with("test-agent", mock.ANY)

    def test_launcher_is_stored_with_path_kept_by_self_registration_and_cleared_by_claude(self):
        self.write_registered()
        with (
            mock.patch.object(module, "_api", return_value={"user_id": "B-test"}),
            mock.patch.object(module, "_announce"),
            mock.patch.dict(os.environ, {"PATH": "/opt/node/bin:/usr/bin"}),
        ):
            module._register("test-agent", "claude", Path("/work"), Path("/profile"), "", None, (),
                             launcher="/opt/bin/ccr cc-work cli --")
            module._register("test-agent", "claude", Path("/work"), Path("/profile"), "", None, ())
            loaded = module.load_agent_configs(self.agents)["test-agent"]
            self.assertEqual((loaded.launcher, loaded.launcher_path),
                             ("/opt/bin/ccr cc-work cli --", "/opt/node/bin:/usr/bin"))
            module._register("test-agent", "claude", Path("/work"), Path("/profile"), "", None, (),
                             launcher="claude")
        loaded = module.load_agent_configs(self.agents)["test-agent"]
        self.assertEqual((loaded.launcher, loaded.launcher_path), ("claude", None))

    def test_rename_carries_the_launcher_from_the_retired_identity(self):
        self.write_registered(name="old-name", session="live-session",
                              CLAUDE_LAUNCHER="/opt/bin/ccr cc-work cli --",
                              CLAUDE_LAUNCHER_PATH="/opt/node/bin")
        with (
            mock.patch.object(module, "_credentials", return_value=credentials("A-new")),
            mock.patch.object(module, "_api", return_value={}),
            mock.patch.object(module, "_announce"),
            mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "live-session"}),
        ):
            module._register("new-name", "claude", Path("/work"), Path("/profile"), "", "T-test", ())
        loaded = module.load_agent_configs(self.agents)["new-name"]
        self.assertEqual((loaded.launcher, loaded.launcher_path),
                         ("/opt/bin/ccr cc-work cli --", "/opt/node/bin"))

    def test_existing_name_kind_change_fails_with_unregister_remedy(self):
        self.write_registered()
        with (
            mock.patch.object(
                module,
                "_api",
                side_effect=module.SlackAPIError("auth.test", "invalid_auth"),
            ) as api,
            mock.patch.object(module, "_credentials") as provision,
            self.assertRaisesRegex(
                module.RegisterError,
                "already registered as claude; unregister the name, then register it with the new kind",
            ),
        ):
            module._register(
                "test-agent", "codex", Path("/work"), Path("/profile"),
                "", None, ("all-agents",),
            )
        provision.assert_not_called()
        api.assert_not_called()

    def test_invalid_or_stale_existing_credentials_reprovision(self):
        for case in ("stale token", "invalid env"):
            with self.subTest(case=case):
                self.write_registered()
                if case == "invalid env":
                    module._write(self.agents / "test-agent.env", "invalid\n")
                api_effect = (
                    module.SlackAPIError("auth.test", "invalid_auth")
                    if case == "stale token"
                    else {"user_id": "B-test"}
                )
                with (
                    mock.patch.object(module, "_api", side_effect=api_effect),
                    mock.patch.object(module, "_credentials", return_value=credentials()) as provision,
                    mock.patch.object(module, "_announce"),
                    mock.patch.object(module.LOG, "warning"),
                ):
                    module._register(
                        "test-agent", "claude", Path("/work"), Path("/profile"),
                        "", "T-test", ("all-agents",),
                    )
                provision.assert_called_once_with(
                    "test-agent", "A-test", "T-test", ("all-agents",), mock.ANY
                )

    def test_registration_announcement_dms_operator_and_logs_failure(self):
        values = credentials()
        with mock.patch.object(module, "_api", side_effect=[
            {"channel": {"id": "D-operator"}}, {"ok": True},
        ]) as api:
            module._announce("test-agent", values)
        self.assertEqual(api.call_args_list, [
            mock.call("conversations.open", "fake-bot-test", users="U-operator"),
            mock.call(
                "chat.postMessage", "fake-bot-test", channel="D-operator",
                text="<@B-test> is online",
            ),
        ])
        with (
            mock.patch.object(module, "_api", side_effect=module.RegisterError("offline")),
            self.assertLogs("slack-register", level="ERROR"),
        ):
            module._announce("test-agent", values)

    def test_service_token_replaces_login_pair_and_is_never_rotated(self):
        pair = {"T-test": {"token": "fake-login", "refresh_token": "fake-refresh", "exp": 0}}
        module._write(self.root / "credentials.json", json.dumps(pair))
        module._write(self.root / "service-token-T-test", "fake-user-service\n")
        with mock.patch.object(module, "_api", return_value={"ok": True}) as api:
            module._user_api("apps.manifest.create", None, json_body=True, manifest="{}")
            module._keep_alive(None)
        self.assertEqual(api.call_args_list, [
            mock.call("apps.manifest.create", "fake-user-service", json_body=True, manifest="{}"),
            mock.call("auth.test", "fake-user-service"),
        ])
        self.assertEqual(json.loads((self.root / "credentials.json").read_text()), pair)

    def test_provision_uses_developer_api_and_joins_channel(self):
        module._write(self.root / "operator.txt", "U-operator\n")
        user_calls = []
        bot_calls = []
        operator_id = ["U-operator"]
        expected_scopes = (
            "chat:write chat:write.public files:write files:read im:history im:write im:read "
            "channels:history channels:join channels:read channels:manage groups:history "
            "groups:read groups:write mpim:history mpim:read mpim:write users:read "
            "reactions:read reactions:write pins:read pins:write"
        ).split()

        def user_api(method, team, **fields):
            user_calls.append((method, team, fields))
            if method == "apps.manifest.create":
                manifest = json.loads(fields["manifest"])
                self.assertEqual(manifest["display_information"]["name"], "test-agent")
                self.assertEqual(manifest["features"]["bot_user"], {
                    "display_name": "test-agent", "always_online": True,
                })
                self.assertEqual(manifest["features"]["app_home"], {
                    "messages_tab_enabled": True,
                    "messages_tab_read_only_enabled": False,
                })
                self.assertEqual(manifest["oauth_config"]["scopes"]["bot"], expected_scopes)
                self.assertEqual(manifest["settings"], {
                    "event_subscriptions": {"bot_events": [
                        "message.im", "message.channels", "message.groups", "message.mpim",
                    ]},
                    "socket_mode_enabled": True,
                    "org_deploy_enabled": False,
                    "token_rotation_enabled": False,
                })
                return {"app_id": "A-test"}
            if method == "apps.developerInstall":
                return {"api_access_tokens": {"bot": "fake-bot-test", "app_level": "xapp-test"}}
            return {"user_id": operator_id[0]}

        def bot_api(method, token=None, **fields):
            bot_calls.append((method, token, fields))
            if method == "auth.test":
                return {"user_id": "B-test"}
            if method == "conversations.list":
                return {"channels": [{"id": "C-test", "name": "all-agents"}]}
            if method == "conversations.info":
                return {"channel": {"is_member": True}}
            return {"ok": True}

        with (
            mock.patch.object(module, "_user_api", side_effect=user_api),
            mock.patch.object(module, "_api", side_effect=bot_api),
        ):
            result = module._provision("test-agent", None, "T-test", ("all-agents",))
        self.assertEqual(result, credentials())
        self.assertEqual([call[0] for call in user_calls], [
            "apps.manifest.create", "apps.developerInstall", "auth.test",
        ])
        self.assertEqual(user_calls[1][2]["bot_scopes"], expected_scopes)
        self.assertTrue(all(call[2]["json_body"] for call in user_calls[:2]))
        self.assertEqual(
            [
                (method, token, fields["channel"])
                for method, token, fields in bot_calls
                if method == "conversations.join"
            ],
            [("conversations.join", "fake-bot-test", "C-test")],
        )
        self.assertEqual(
            [
                (method, token, fields["channel"])
                for method, token, fields in bot_calls
                if method == "conversations.info"
            ],
            [("conversations.info", "fake-bot-test", "C-test")],
        )
        operator_id[0] = "U-other"
        with (
            mock.patch.object(module, "_user_api", side_effect=user_api),
            mock.patch.object(module, "_api", side_effect=bot_api),
            self.assertRaises(module.RegisterError),
        ):
            module._provision("test-agent", "A-test", "T-test", ())
        self.assertEqual(user_calls[-3][0], "apps.manifest.update")
        self.assertTrue(user_calls[-3][2]["json_body"])

    def test_join_rejects_unconfirmed_membership(self):
        def bot_api(method, token, **fields):
            if method == "conversations.list":
                return {"channels": [{"id": "C-test", "name": "test-channel"}]}
            if method == "conversations.info":
                return {"channel": {"is_member": False}}
            return {"ok": True}

        with (
            mock.patch.object(module, "_api", side_effect=bot_api),
            self.assertRaises(module.RegisterError),
        ):
            module._join_channels(credentials(), ("test-channel",))

    def test_deleted_recorded_app_is_recreated_on_reuse(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        module._write(self.root / "operator.txt", "U-operator\n")
        calls = []

        def user_api(method, team, json_body=False, **fields):
            calls.append(method)
            if method == "apps.manifest.update":
                raise module.SlackAPIError(method, "app_not_found")
            if method == "apps.manifest.create":
                return {"app_id": "A-fresh"}
            if method == "apps.developerInstall":
                return {"api_access_tokens": {"app_level": "xapp", "bot": "xoxb"}}
            if method == "auth.test":
                return {"user_id": "U-operator"}
            return {}

        def bot_api(method, token, json_body=False, **fields):
            if method == "auth.test":
                return {"user_id": "B-bot", "app_id": "A-fresh"}
            return {"channels": []}

        with (
            mock.patch.object(module, "_user_api", side_effect=user_api),
            mock.patch.object(module, "_api", side_effect=bot_api),
        ):
            result = module._provision("test-agent", "A-deleted", "T-test", ())
        self.assertEqual(result["SLACK_APP_ID"], "A-fresh")
        self.assertEqual(
            calls[:3],
            ["apps.manifest.update", "apps.manifest.create", "apps.developerInstall"],
        )

    def test_cli_token_refresh_uses_form_and_atomically_updates_credentials(self):
        self.root.mkdir(mode=0o700)
        original = {
            "T-test": {
                "token": "user-old", "refresh_token": "refresh-old", "exp": 1,
                "last_updated": 2, "team_domain": "test", "team_id": "T-test",
                "user_id": "U-operator",
            }
        }
        module._write(module.SLACK_CREDENTIALS, json.dumps(original))
        response = mock.MagicMock()
        response.__enter__.return_value = mock.Mock()
        response.__enter__.return_value.read.return_value = (
            b'{"ok":true,"token":"user-new","refresh_token":"refresh-new","exp":3}'
        )
        with mock.patch.object(module.urllib.request, "urlopen", return_value=response) as open_url:
            rotated = module._api("tooling.tokens.rotate", refresh_token="refresh-old")
            module._api(
                "apps.developerInstall", "user-old", json_body=True,
                app_id="A-test", bot_scopes=["chat:write"],
            )
        form_request = open_url.call_args_list[0].args[0]
        json_request = open_url.call_args_list[1].args[0]
        self.assertEqual(form_request.data, b"refresh_token=refresh-old")
        self.assertEqual(
            json.loads(json_request.data),
            {"app_id": "A-test", "bot_scopes": ["chat:write"]},
        )
        self.assertEqual(json_request.get_header("Content-type"), "application/json")
        with mock.patch.object(module, "_api", return_value=rotated):
            entry = module._refresh_user_token("T-test", original["T-test"])
        saved = json.loads(module._read(module.SLACK_CREDENTIALS))
        self.assertEqual(entry["token"], "user-new")
        self.assertEqual(saved["T-test"]["refresh_token"], "refresh-new")
        self.assertEqual(saved["T-test"]["exp"], 3)
        self.assertEqual(saved["T-test"]["last_updated"], 2)
        self.assertEqual(stat.S_IMODE(module.SLACK_CREDENTIALS.stat().st_mode), 0o600)

    def test_concurrent_cli_rotation_is_adopted_without_overwrite(self):
        self.root.mkdir(mode=0o700)
        stale = {"token": "old", "refresh_token": "refresh-old"}
        latest = {"T-test": {"token": "other", "refresh_token": "refresh-other"}}
        module._write(module.SLACK_CREDENTIALS, json.dumps(latest))
        with (
            mock.patch.object(module, "_api", return_value={
                "token": "ours", "refresh_token": "refresh-ours",
            }),
            mock.patch.object(module.pty_broker, "atomic_write") as write,
        ):
            adopted = module._refresh_user_token("T-test", stale)
        self.assertEqual(adopted, latest["T-test"])
        write.assert_not_called()

    def test_user_api_refreshes_once_after_expired_token(self):
        self.root.mkdir(mode=0o700)
        module._write(module.SLACK_CREDENTIALS, json.dumps({
            "T-test": {"token": "old", "refresh_token": "refresh-old"},
        }))
        with (
            mock.patch.object(module, "_api", side_effect=[
                module.SlackAPIError("auth.test", "token_expired"),
                {"user_id": "U-operator"},
            ]) as api,
            mock.patch.object(module, "_refresh_user_token", return_value={
                "token": "new", "refresh_token": "refresh-new",
            }) as refresh,
        ):
            result = module._user_api("auth.test", "T-test")
        self.assertEqual(result["user_id"], "U-operator")
        refresh.assert_called_once()
        self.assertEqual([call.args[1] for call in api.call_args_list], ["old", "new"])

    def test_provision_failure_writes_no_identity(self):
        with mock.patch.object(module, "_credentials", side_effect=module.RegisterError("provision failed")):
            with self.assertRaises(module.RegisterError):
                module._register(
                    "test-agent",
                    "claude",
                    Path("/work"),
                    Path("/profile"),
                    "",
                    None,
                    ("all-agents",),
                )
        self.assertFalse((self.root / "registry.json").exists())
        self.assertFalse((self.agents / "test-agent.env").exists())

    async def test_rename_keeps_the_same_app_then_unregister_tombstones_it(self):
        self.write_registered("old-agent", "A-old", "live-session")
        current = module.load_registry(self.root / "registry.json")
        current["tombstones"]["new-agent"] = {
            "app_id": "A-retired", "session_id": None, "last_registered": 8,
        }
        module._save(current)
        module._write(module.SLACK_CREDENTIALS, json.dumps({
            "T-other": {"token": "user-other", "refresh_token": "refresh-other"},
            "T-rename": {"token": "user-rename", "refresh_token": "refresh-rename"},
        }))
        module._write(self.root / "operator.txt", "U-operator\n")
        calls = []

        def slack_api(method, token=None, json_body=False, **fields):
            calls.append((method, token, fields))
            if method == "auth.test":
                if token == "fake-bot-test":
                    return {"team_id": "T-rename", "user_id": "B-test"}
                if token == "fake-bot-renamed":
                    return {"user_id": "B-renamed"}
                if token == "user-rename":
                    return {"user_id": "U-operator"}
            if method == "apps.developerInstall":
                return {"api_access_tokens": {"app_level": "xapp-renamed", "bot": "fake-bot-renamed"}}
            if method in {"apps.manifest.delete", "apps.manifest.update"}:
                return {"ok": True}
            raise AssertionError(f"unexpected Slack call: {method} ({token})")

        with mock.patch.object(module, "_api", side_effect=slack_api):
            can_post = await module.rename_agent("old-agent", "new-agent")
        self.assertFalse(can_post)
        expected_mutations = {
            "apps.manifest.delete", "apps.manifest.update", "apps.developerInstall",
        }
        mutation_calls = [
            (method, token) for method, token, _ in calls if method in expected_mutations
        ]
        self.assertTrue(expected_mutations <= {method for method, _ in mutation_calls})
        self.assertTrue(all(token == "user-rename" for _, token in mutation_calls))
        self.assertFalse(any(token == "user-other" for _, token, _ in calls))
        renamed = module.load_agent_configs(self.agents)["new-agent"]
        self.assertEqual(renamed.session_id, "live-session")
        self.assertEqual(renamed.bot_token, "fake-bot-renamed")
        after_rename = module.load_registry(self.root / "registry.json")
        self.assertEqual(after_rename["agents"]["new-agent"]["app_id"], "A-old")
        self.assertEqual(after_rename["tombstones"], {})
        self.assertFalse((self.agents / "old-agent.env").exists())

        await module.unregister_agent("new-agent")
        after_unregister = module.load_registry(self.root / "registry.json")
        self.assertEqual(after_unregister["agents"], {})
        self.assertIsNone(after_unregister["tombstones"]["new-agent"]["session_id"])
        self.assertFalse((self.agents / "new-agent.env").exists())

    async def test_rename_rejects_missing_workspace_before_mutation(self):
        self.write_registered("old-agent", "A-old", "live-session")
        with (
            mock.patch.object(module, "_api", return_value={"user_id": "B-test"}) as api,
            mock.patch.object(module, "_provision", return_value=credentials("A-old")) as provision,
            self.assertRaises(module.RegisterError),
        ):
            await module.rename_agent("old-agent", "new-agent")
        api.assert_called_once_with("auth.test", "fake-bot-test")
        provision.assert_not_called()
        self.assertTrue((self.agents / "old-agent.env").exists())
        self.assertFalse((self.agents / "new-agent.env").exists())
        saved = module.load_registry(self.root / "registry.json")
        self.assertIn("old-agent", saved["agents"])
        self.assertNotIn("new-agent", saved["agents"])

    def test_keep_alive_rotates_only_the_near_expiry_workspace(self):
        now = 1_000_000
        original = {
            "T-far": {"token": "far-old", "refresh_token": "far-refresh", "exp": now + 8 * 3600},
            "T-near": {"token": "near-old", "refresh_token": "near-refresh", "exp": now + 3600},
        }
        module._write(module.SLACK_CREDENTIALS, json.dumps(original, indent=2) + "\n")
        calls = []

        def fake_api(method, token=None, **fields):
            calls.append((method, token, fields))
            if method == "tooling.tokens.rotate":
                self.assertEqual(fields["refresh_token"], "near-refresh")
                return {"token": "near-new", "refresh_token": "near-refresh-new", "exp": now + 24 * 3600}
            self.assertEqual(method, "auth.test")
            return {"ok": True}

        with (
            mock.patch.object(module, "_api", side_effect=fake_api),
            mock.patch.object(module.time, "time", return_value=now),
        ):
            module._keep_alive(None)
        saved = json.loads(module._read(module.SLACK_CREDENTIALS))
        self.assertEqual([call[0] for call in calls].count("tooling.tokens.rotate"), 1)
        self.assertEqual(saved["T-far"], original["T-far"])
        self.assertEqual(saved["T-near"], {
            "token": "near-new", "refresh_token": "near-refresh-new", "exp": now + 24 * 3600,
        })

    def test_keep_alive_preserves_concurrent_workspace_rotations(self):
        now = 1_000_000
        module._write(module.SLACK_CREDENTIALS, json.dumps({
            "T-one": {"token": "one-old", "refresh_token": "one-refresh", "exp": now + 1},
            "T-two": {"token": "two-old", "refresh_token": "two-refresh", "exp": now + 1},
        }))
        start = threading.Barrier(2)
        rotations = threading.Barrier(2)
        writes = threading.Barrier(2)
        real_atomic_write = module.pty_broker.atomic_write
        failures = []

        def fake_api(method, token=None, **fields):
            if method == "tooling.tokens.rotate":
                try:
                    rotations.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass
                team = fields["refresh_token"].removesuffix("-refresh")
                return {
                    "token": f"{team}-new",
                    "refresh_token": f"{team}-refresh-new",
                    "exp": now + 24 * 3600,
                }
            self.assertEqual(method, "auth.test")
            if token == "two-old":
                raise module.SlackAPIError(method, "token_expired")
            return {"ok": True}

        def fake_atomic_write(path, text):
            if path == module.SLACK_CREDENTIALS:
                try:
                    writes.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass
            real_atomic_write(path, text)

        def keep_alive():
            try:
                start.wait(timeout=1)
                module._keep_alive("T-one")
            except BaseException as exc:
                failures.append(exc)

        def user_api():
            try:
                start.wait(timeout=1)
                module._user_api("auth.test", "T-two")
            except BaseException as exc:
                failures.append(exc)

        with (
            mock.patch.object(module, "_api", side_effect=fake_api),
            mock.patch.object(module.pty_broker, "atomic_write", side_effect=fake_atomic_write),
            mock.patch.object(module.time, "time", return_value=now),
        ):
            threads = [threading.Thread(target=keep_alive), threading.Thread(target=user_api)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=4)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        saved = json.loads(module._read(module.SLACK_CREDENTIALS))
        self.assertEqual(saved["T-one"]["token"], "one-new")
        self.assertEqual(saved["T-two"]["token"], "two-new")

    def test_keep_alive_renews_other_workspace_after_rotate_failure(self):
        now = 1_000_000
        original = {
            "T-one": {"token": "one-old", "refresh_token": "one-refresh", "exp": now + 1},
            "T-two": {"token": "two-old", "refresh_token": "two-refresh", "exp": now + 1},
        }
        module._write(module.SLACK_CREDENTIALS, json.dumps(original))

        def fake_api(method, token=None, **fields):
            if method == "tooling.tokens.rotate":
                if fields["refresh_token"] == "one-refresh":
                    raise module.SlackAPIError(method, "invalid_refresh_token")
                return {
                    "token": "two-new", "refresh_token": "two-refresh-new",
                    "exp": now + 24 * 3600,
                }
            self.assertEqual(method, "auth.test")
            return {"ok": True}

        with (
            mock.patch.object(module, "_api", side_effect=fake_api),
            mock.patch.object(module.time, "time", return_value=now),
            self.assertRaises(module.RegisterError),
        ):
            module._keep_alive(None)
        saved = json.loads(module._read(module.SLACK_CREDENTIALS))
        self.assertEqual(saved["T-one"], original["T-one"])
        self.assertEqual(saved["T-two"]["token"], "two-new")

    def test_keep_alive_uses_selected_cli_credential_without_restart(self):
        module._write(module.SLACK_CREDENTIALS, json.dumps({
            "T-test": {
                "token": "user-test", "refresh_token": "refresh-test",
                "exp": int(module.time.time()) + module.KEEP_ALIVE_ROTATE_WITHIN + 1,
            },
        }))
        with mock.patch.object(module, "_api", return_value={"ok": True}) as api:
            module._keep_alive(None)
        api.assert_called_once_with("auth.test", "user-test")

        with mock.patch.object(module, "_keep_alive") as keep_alive:
            result = module.run(["--keep-alive", "--team", "T-test"])
        self.assertEqual(result, 0)
        keep_alive.assert_called_once_with("T-test")

    def test_unregister_reports_when_the_bridge_picks_up_the_change(self):
        with (
            mock.patch.object(module, "_unregister") as unregister,
            mock.patch("builtins.print") as printed,
        ):
            result = module.run(["--unregister", "test-agent"])
        self.assertEqual(result, 0)
        unregister.assert_called_once_with("test-agent")
        printed.assert_called_once_with("slack-bridge picks up test-agent within 5 s")

    def test_cli_claude_registration_records_live_model_and_effort_unless_overridden(self):
        # No --claude-args: the live claude ancestor's --model/--effort are
        # recorded, so a later revive resumes at the same tier.
        with (
            mock.patch.object(module, "_register") as register,
            mock.patch.object(module, "_live_claude_args", return_value="--model claude-fable-5-1 --effort high"),
            mock.patch("builtins.print"),
        ):
            module.run([
                "test-claude", "--kind", "claude", "--workdir", "/work",
                "--claude-config-dir", "/profile",
            ])
        register.assert_called_once_with(
            "test-claude", "claude", Path("/work"), Path("/profile"),
            "--model claude-fable-5-1 --effort high", None, ("all-agents",), launcher=None,
        )

        # An explicit --claude-args is kept, not overwritten by the live process.
        with (
            mock.patch.object(module, "_register") as register,
            mock.patch.object(module, "_live_claude_args", return_value="--model claude-fable-5-1 --effort high"),
            mock.patch("builtins.print"),
        ):
            module.run([
                "test-claude", "--kind", "claude", "--workdir", "/work",
                "--claude-config-dir", "/profile", "--claude-args=--model sonnet",
            ])
        register.assert_called_once_with(
            "test-claude", "claude", Path("/work"), Path("/profile"),
            "--model sonnet", None, ("all-agents",), launcher=None,
        )

    def test_live_claude_args_quote_a_model_name_with_a_space(self):
        # A router session's model name holds a space; revive runs the stored
        # args through a shell, so they must stay one word each.
        with (
            mock.patch.object(module.os, "getppid", return_value=42),
            mock.patch.object(module.Path, "read_text", return_value="claude\n"),
            mock.patch.object(module.slack_live_delivery, "_parent_flag", return_value="Gemini API,gemini-3.8-flash"),
            mock.patch.object(module.slack_live_delivery, "_parent_effort", return_value=["--effort", "high"]),
        ):
            self.assertEqual(module._live_claude_args(), "--model 'Gemini API,gemini-3.8-flash' --effort high")

    def test_cli_parses_codex_profile_without_external_effects(self):
        with (
            mock.patch.object(module, "_register") as register,
            mock.patch("builtins.print") as printed,
        ):
            result = module.run(
                [
                    "test-codex",
                    "--kind",
                    "codex",
                    "--workdir",
                    "/work",
                    "--codex-home",
                    "/profile",
                    "--codex-args=--fast",
                    "--join",
                    "one",
                    "--join",
                    "two",
                ]
            )
        self.assertEqual(result, 0)
        register.assert_called_once_with(
            "test-codex",
            "codex",
            Path("/work"),
            Path("/profile"),
            "--fast",
            None,
            ("all-agents", "one", "two"),
            launcher=None,
        )
        printed.assert_called_once_with("slack-bridge picks up test-codex within 5 s")

    def test_cli_parses_antigravity_without_profile_flag(self):
        with (
            mock.patch.object(module, "_register") as register,
            mock.patch("builtins.print") as printed,
        ):
            result = module.run([
                "test-antigravity", "--kind", "antigravity", "--workdir", "/work",
            ])
        self.assertEqual(result, 0)
        register.assert_called_once_with(
            "test-antigravity", "antigravity", Path("/work"),
            module.ANTIGRAVITY_PROFILE_DIR, "", None, ("all-agents",), launcher=None,
        )
        printed.assert_called_once_with(
            "slack-bridge picks up test-antigravity within 5 s"
        )

    def test_antigravity_registration_writes_profile_and_advertised_session(self):
        conversation_id = "658e78b0-9cff-48ec-b427-b4885bdfcd50"
        module._write(
            self.pty / "12345.json",
            json.dumps({
                "broker_pid": 12345,
                "socket": str(self.pty / "12345.sock"),
                "pgid": 12346,
                "proc_start": "1",
                "kind": "interactive",
                "child_pid": 12346,
                "child_proc_start": "2",
                "token": "token",
                "conversation_id": conversation_id,
            }),
        )
        with (
            mock.patch.object(module, "_credentials", return_value=credentials()),
            mock.patch.object(module, "_announce"),
            mock.patch.dict(os.environ, {"ANTIGRAVITY_PTY_BROKER_PID": "12345"}, clear=True),
        ):
            module._register(
                "test-antigravity",
                "antigravity",
                Path("/work"),
                None,
                "",
                None,
                ("all-agents",),
            )

        self.assertEqual(
            module._env(self.agents / "test-antigravity.env"),
            credentials() | {
                "AGENT_KIND": "antigravity",
                "WORKDIR": "/work",
                "DELIVER_CHANNEL_MESSAGES": "false",
            },
        )
        loaded = module.load_agent_configs(self.agents)["test-antigravity"]
        self.assertEqual(loaded.profile_dir, module.ANTIGRAVITY_PROFILE_DIR)
        self.assertEqual(loaded.runtime_args, "")
        saved = module.load_registry(self.root / "registry.json")
        self.assertEqual(
            saved["agents"]["test-antigravity"]["session_id"], conversation_id,
        )

    def test_antigravity_registration_requires_advertised_session(self):
        with (
            mock.patch.object(module, "_credentials", return_value=credentials()) as provision,
            mock.patch.dict(os.environ, {"ANTIGRAVITY_PTY_BROKER_PID": "12345"}, clear=True),
            self.assertRaises(module.RegisterError),
        ):
            module._register(
                "test-antigravity", "antigravity", Path("/work"), Path("/profile"),
                "", None, ("all-agents",),
            )
        provision.assert_not_called()
        module._write(self.pty / "12345.json", json.dumps({"broker_pid": 12345}))
        with (
            mock.patch.object(module, "_credentials", return_value=credentials()) as provision,
            mock.patch.dict(os.environ, {"ANTIGRAVITY_PTY_BROKER_PID": "12345"}, clear=True),
            self.assertRaises(module.RegisterError),
        ):
            module._register(
                "test-antigravity", "antigravity", Path("/work"), Path("/profile"),
                "", None, ("all-agents",),
            )
        provision.assert_not_called()
        self.assertFalse((self.agents / "test-antigravity.env").exists())


if __name__ == "__main__":
    unittest.main()
