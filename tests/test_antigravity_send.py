import importlib.util
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
MODULE_PATH = TOOLS / "antigravity-send.py"
SPEC = importlib.util.spec_from_file_location("antigravity_send_tested", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class AntigravitySendTest(unittest.TestCase):
    def test_live_advertisement_delivers_and_dead_target_raises_lookup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "pty"
            proc_root = root / "proc"
            registry_path = root / "registry.json"
            state_dir.mkdir(mode=0o700)
            self._write_registry(
                registry_path,
                active={"test-antigravity-peer": "00000000-0000-0000-0000-000000000001"},
            )
            self._write_process(proc_root, 4101, 1, "71")
            self._write_process(proc_root, 4102, 4101, "72")

            socket_path = state_dir / "4101.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            socket_path.chmod(0o600)
            listener.listen(1)
            received = []

            def serve():
                with listener, listener.accept()[0] as connection:
                    payload = bytearray()
                    while chunk := connection.recv(4096):
                        payload.extend(chunk)
                    received.append(json.loads(payload))
                    connection.sendall(b"OK\n")

            server = threading.Thread(target=serve)
            server.start()
            self._write_advertisement(
                state_dir / "4101.json",
                broker_pid=4101,
                socket=str(socket_path),
                pgid=4102,
                proc_start="71",
                kind="interactive",
                child_pid=4102,
                child_proc_start="72",
                token="test-token",
                conversation_id="00000000-0000-0000-0000-000000000001",
                agent_name="test-antigravity-peer",
            )

            result = module.send_message(
                "test-antigravity-peer",
                "check this result",
                state_dir=state_dir,
                proc_root=proc_root,
                registry_path=registry_path,
            )
            server.join(timeout=2)
            self.assertFalse(server.is_alive())
            self.assertEqual(
                received,
                [{"token": "test-token", "target_pid": 4102, "text": "check this result"}],
            )
            self.assertEqual(result["transport"], "pty")

            self._write_advertisement(
                state_dir / "4201.json",
                broker_pid=4201,
                socket=str(state_dir / "4201.sock"),
                pgid=4202,
                proc_start="81",
                kind="interactive",
                child_pid=4202,
                child_proc_start="82",
                token="dead-token",
                conversation_id="00000000-0000-0000-0000-000000000002",
                agent_name="test-dead-antigravity-peer",
            )
            with self.assertRaises(LookupError):
                module.send_message(
                    "00000000-0000-0000-0000-000000000002",
                    "work",
                    state_dir=state_dir,
                    proc_root=proc_root,
                    registry_path=registry_path,
                )

    def test_retired_advertised_name_no_longer_resolves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "pty"
            registry_path = root / "registry.json"
            state_dir.mkdir(mode=0o700)
            advertisement_path = state_dir / "5101.json"
            self._write_advertisement(
                advertisement_path,
                agent_name="test-retired-antigravity-peer",
                conversation_id="00000000-0000-0000-0000-000000000003",
            )
            self._write_registry(
                registry_path,
                active={"test-retired-antigravity-peer": "00000000-0000-0000-0000-000000000003"},
            )
            self.assertEqual(
                module._resolve_advertisement(
                    "test-retired-antigravity-peer", state_dir, registry_path
                )[0],
                advertisement_path,
            )

            self._write_registry(
                registry_path, retired=("test-retired-antigravity-peer",)
            )
            with self.assertRaises(LookupError):
                module._resolve_advertisement(
                    "test-retired-antigravity-peer", state_dir, registry_path
                )
            self.assertEqual(
                module._resolve_advertisement(
                    "00000000-0000-0000-0000-000000000003",
                    state_dir,
                    registry_path,
                )[0],
                advertisement_path,
            )

    def test_uuid_name_and_conversation_collision_requires_one_advertisement(self):
        target = "00000000-0000-0000-0000-000000000004"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "pty"
            registry_path = root / "registry.json"
            state_dir.mkdir(mode=0o700)
            self._write_registry(
                registry_path,
                active={target: "00000000-0000-0000-0000-000000000005"},
            )
            conversation_advertisement = state_dir / "6101.json"
            name_advertisement = state_dir / "6102.json"
            self._write_advertisement(
                conversation_advertisement,
                agent_name="test-other-antigravity-peer",
                conversation_id=target,
            )
            self._write_advertisement(
                name_advertisement,
                agent_name=target,
                conversation_id="00000000-0000-0000-0000-000000000005",
            )
            with self.assertRaises(LookupError):
                module._resolve_advertisement(target, state_dir, registry_path)

            name_advertisement.unlink()
            self._write_registry(registry_path, active={target: target})
            self._write_advertisement(
                conversation_advertisement,
                agent_name=target,
                conversation_id=target,
            )
            self.assertEqual(
                module._resolve_advertisement(target, state_dir, registry_path)[0],
                conversation_advertisement,
            )

    @staticmethod
    def _write_process(proc_root: Path, pid: int, parent: int, start: str) -> None:
        process_dir = proc_root / str(pid)
        process_dir.mkdir(parents=True)
        fields = ["S", str(parent), *(["0"] * 17), start]
        (process_dir / "stat").write_text(f"{pid} (test) {' '.join(fields)}\n")

    @staticmethod
    def _write_advertisement(path: Path, **advertisement) -> None:
        path.write_text(json.dumps(advertisement))
        path.chmod(0o600)

    @staticmethod
    def _write_registry(path: Path, active=None, retired=None) -> None:
        def records(sessions):
            return {
                name: {
                    "app_id": f"app-{index}",
                    "session_id": session,
                    "last_registered": index,
                }
                for index, (name, session) in enumerate(sessions.items(), 1)
            }

        active = active or {}
        retired = retired or {}
        if not isinstance(retired, dict):
            retired = dict.fromkeys(retired)
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "machine_id": "test-machine",
                    "agents": records(active),
                    "tombstones": records(retired),
                }
            )
        )
        path.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
