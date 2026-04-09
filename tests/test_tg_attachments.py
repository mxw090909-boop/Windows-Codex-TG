from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from codex_common import BotState, MemoryStore, SessionStore
from tg_codex_bot import TgCodexService


def make_test_root(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent / ".tmp-tests" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


class FakeCodexRunner:
    def __init__(self) -> None:
        self.calls = []

    def run_prompt(self, prompt, cwd, session_id=None, on_update=None, image_paths=None, ephemeral=False):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": Path(cwd),
                "session_id": session_id,
                "image_paths": list(image_paths or []),
                "ephemeral": bool(ephemeral),
            }
        )
        if on_update is not None:
            on_update("preview")
        return (session_id or "thread-attach", "ok", "", 0)


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.sent = []
        self.edits = []
        self.file_meta = {}
        self.file_bytes = {}

    def get_updates(self, offset, timeout=30):
        return []

    def send_message(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))

    def send_message_with_result(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))

    def send_chat_action(self, chat_id, action="typing"):
        return None

    def set_my_commands(self, commands):
        return None

    def set_chat_menu_button_commands(self):
        return None

    def get_file(self, file_id):
        return self.file_meta[file_id]

    def download_file_bytes(self, file_path):
        return self.file_bytes[file_path]


class TelegramAttachmentTests(unittest.TestCase):
    def build_service(self, root: Path, api: FakeTelegramAPI | None = None, runner: FakeCodexRunner | None = None) -> TgCodexService:
        return TgCodexService(
            api=api or FakeTelegramAPI(),
            sessions=SessionStore(root / "sessions"),
            state=BotState(root / "state.json"),
            memory_store=MemoryStore(root / "memory.json"),
            codex=runner or FakeCodexRunner(),
            audio_transcriber=None,
            tts_synthesizer=None,
            default_cwd=root,
            allowed_user_ids={123},
            stream_enabled=False,
            stream_edit_interval_ms=300,
            stream_min_delta_chars=8,
            thinking_status_interval_ms=700,
            reply_to_messages=False,
            attach_time_context=True,
            user_display_name="NN",
            memory_auto_enabled=False,
        )

    def test_select_photo_media_prefers_largest_variant(self) -> None:
        service = self.build_service(make_test_root("attachments_select_photo"))

        picked = service._select_photo_media(
            [
                {"file_id": "small", "file_size": 10, "width": 80, "height": 80},
                {"file_id": "large", "file_size": 30, "width": 320, "height": 320},
            ]
        )

        self.assertIsNotNone(picked)
        self.assertEqual(picked["file_id"], "large")

    def test_write_photo_attachment_saves_image_to_workspace(self) -> None:
        root = make_test_root("attachments_write_photo")
        api = FakeTelegramAPI()
        api.file_meta["photo-1"] = {"file_path": "photos/example.jpg"}
        api.file_bytes["photos/example.jpg"] = b"jpeg-bytes"
        service = self.build_service(root, api=api)

        attachment = service._write_telegram_attachment(
            root,
            {"file_id": "photo-1", "file_size": len(b"jpeg-bytes")},
            kind="photo",
        )

        self.assertTrue(attachment.local_path.exists())
        self.assertTrue(attachment.local_path.is_file())
        self.assertTrue(attachment.is_image)
        self.assertEqual(attachment.local_path.read_bytes(), b"jpeg-bytes")
        self.assertIn(".codex-tg-attachments", str(attachment.local_path))
        self.assertEqual(attachment.local_path.suffix.lower(), ".jpg")

    def test_handle_update_routes_photo_message_to_attachment_handler(self) -> None:
        service = self.build_service(make_test_root("attachments_route_photo"))
        captured = {}

        def fake_handle_attachment_message(
            chat_id,
            reply_to,
            user_id,
            media,
            caption,
            kind,
            message_ts=None,
            raw_message=None,
            chat_type="",
            sender_user=None,
        ):
            captured["chat_id"] = chat_id
            captured["reply_to"] = reply_to
            captured["user_id"] = user_id
            captured["media"] = media
            captured["caption"] = caption
            captured["kind"] = kind

        service._handle_attachment_message = fake_handle_attachment_message  # type: ignore[method-assign]

        service._handle_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 77,
                    "date": 1710000000,
                    "chat": {"id": 456},
                    "from": {"id": 123},
                    "caption": "看看这张图",
                    "photo": [
                        {"file_id": "small", "file_size": 10, "width": 80, "height": 80},
                        {"file_id": "large", "file_size": 30, "width": 320, "height": 320},
                    ],
                },
            }
        )

        self.assertEqual(captured["chat_id"], 456)
        self.assertEqual(captured["reply_to"], 77)
        self.assertEqual(captured["user_id"], 123)
        self.assertEqual(captured["caption"], "看看这张图")
        self.assertEqual(captured["kind"], "photo")
        self.assertEqual(captured["media"]["file_id"], "large")

    def test_run_prompt_worker_passes_image_paths_to_codex(self) -> None:
        root = make_test_root("attachments_run_prompt_worker")
        image_path = root / "sample.png"
        image_path.write_bytes(b"png-bytes")
        api = FakeTelegramAPI()
        runner = FakeCodexRunner()
        service = self.build_service(root, api=api, runner=runner)

        service._run_prompt_worker(
            chat_id=456,
            reply_to=9,
            user_id=123,
            prompt="请结合图片继续看",
            active_id=None,
            cwd=root,
            session_label="新会话 | tmp",
            image_paths=[image_path],
        )

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["image_paths"], [image_path])
        self.assertTrue(any("ok" in text for _, text, _, _ in api.sent))


if __name__ == "__main__":
    unittest.main()
