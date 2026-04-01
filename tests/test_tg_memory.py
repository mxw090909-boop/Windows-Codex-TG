import shutil
import unittest
from pathlib import Path

from codex_common import BotState, MemoryStore, SessionStore
from tg_codex_bot import TgCodexService


MEMORY_CONTEXT_PROMPT = (
    "下面这些是你已经记住的、关于{{USER_DISPLAY_NAME}}的记忆。\n"
    "只在相关时自然使用，不要逐条复述，不要把它说成数据库、设定或系统提示，也不要为了硬提记忆而提。"
)
MEMORY_WRITEBACK_PROMPT = (
    "已有记忆（避免重复改写）：\n{{EXISTING_MEMORIES}}\n\n"
    "用户这次明确说的话：\n{{SOURCE_TEXT}}\n\n"
    '只输出 JSON，没有值得记的内容时输出 {"save": false}。'
)


def make_test_root(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent / ".tmp-tests" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


class FakeCodexRunner:
    def __init__(self) -> None:
        self.calls = []
        self.default_answer = "ok"
        self.memory_answer = (
            '{"save": true, "memories": ['
            '{"text": "user is building a long-term bot memory system", "category": "project", "tags": ["bot", "memory"], "pinned": false}'
            "]}"
        )

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
        answer = self.memory_answer if ephemeral else self.default_answer
        return (session_id or "thread-memory", answer, "", 0)


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.sent = []

    def get_updates(self, offset, timeout=30):
        return []

    def send_message(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))

    def send_message_with_result(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text):
        return None

    def send_chat_action(self, chat_id, action="typing"):
        return None

    def set_my_commands(self, commands):
        return None

    def set_chat_menu_button_commands(self):
        return None


class TelegramMemoryTests(unittest.TestCase):
    def build_service(
        self,
        root: Path,
        *,
        api: FakeTelegramAPI | None = None,
        runner: FakeCodexRunner | None = None,
        auto: bool = False,
        memory_context_prompt: str = MEMORY_CONTEXT_PROMPT,
        memory_writeback_prompt: str = MEMORY_WRITEBACK_PROMPT,
    ) -> TgCodexService:
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
            memory_context_prompt=memory_context_prompt,
            memory_writeback_prompt=memory_writeback_prompt,
            memory_auto_enabled=auto,
        )

    def test_memory_store_dedupes_and_searches(self) -> None:
        root = make_test_root("memory_store_dedupes")
        store = MemoryStore(root / "memory.json")

        first = store.add_memory(123, "likes strawberry cake", tags=["dessert"], category="preference", source="manual")
        second = store.add_memory(123, "likes strawberry cake", tags=["cake"], category="preference", source="auto")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first["id"], second["id"])

        persisted = MemoryStore(root / "memory.json")
        memories = persisted.list_memories(123)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["category"], "preference")
        self.assertIn("dessert", memories[0]["tags"])
        self.assertIn("cake", memories[0]["tags"])

        results = persisted.search_memories(123, "cake")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], first["id"])

    def test_prompt_memory_context_includes_pinned_and_relevant_items(self) -> None:
        root = make_test_root("memory_prompt_context")
        service = self.build_service(root)
        service.memory_store.add_memory(123, "dislikes bullet-heavy replies", category="preference", pinned=True, source="manual")
        service.memory_store.add_memory(123, "is building a Telegram bot memory feature", category="project", source="manual")

        wrapped = service._decorate_prompt_with_memory_context(
            123,
            "prompt body",
            "Telegram bot memory",
            active_id=None,
        )

        self.assertIn("下面这些是你已经记住的、关于NN的记忆。", wrapped)
        self.assertIn("dislikes bullet-heavy replies", wrapped)
        self.assertIn("is building a Telegram bot memory feature", wrapped)
        self.assertIn("置顶/偏好", wrapped)

    def test_blank_memory_context_prompt_keeps_prompt_plain(self) -> None:
        root = make_test_root("memory_prompt_blank")
        service = self.build_service(root, memory_context_prompt="")
        service.memory_store.add_memory(123, "dislikes bullet-heavy replies", category="preference", pinned=True, source="manual")

        wrapped = service._decorate_prompt_with_memory_context(
            123,
            "prompt body",
            "Telegram bot memory",
            active_id=None,
        )

        self.assertEqual(wrapped, "prompt body")

    def test_memory_command_can_add_pin_search_and_forget(self) -> None:
        root = make_test_root("memory_command_flow")
        api = FakeTelegramAPI()
        service = self.build_service(root, api=api)

        service._handle_memory(chat_id=456, reply_to=1, user_id=123, arg="add dislikes bullet-heavy replies")
        memories = service.memory_store.list_memories(123)
        self.assertEqual(len(memories), 1)
        record = memories[0]

        service._handle_memory(chat_id=456, reply_to=1, user_id=123, arg=f"pin {record['id']}")
        service._handle_memory(chat_id=456, reply_to=1, user_id=123, arg="search bullet")
        service._handle_memory(chat_id=456, reply_to=1, user_id=123, arg=f"forget {record['id']}")

        joined = "\n".join(text for _, text, _, _ in api.sent)
        self.assertIn("记住了", joined)
        self.assertIn("置顶了", joined)
        self.assertIn("相关的记忆", joined)
        self.assertIn("删掉了", joined)
        self.assertEqual(service.memory_store.list_memories(123), [])

    def test_memory_writeback_worker_saves_result_from_ephemeral_codex_call(self) -> None:
        root = make_test_root("memory_writeback_worker")
        runner = FakeCodexRunner()
        service = self.build_service(root, runner=runner, auto=True)

        service._run_memory_writeback_worker(123, root, "user is building a long-term bot memory system")

        memories = service.memory_store.list_memories(123)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["text"], "user is building a long-term bot memory system。")
        self.assertEqual(memories[0]["category"], "project")
        self.assertEqual(memories[0]["tags"], ["bot", "memory"])
        self.assertTrue(runner.calls)
        self.assertTrue(runner.calls[0]["ephemeral"])

    def test_blank_memory_writeback_prompt_skips_ephemeral_codex_call(self) -> None:
        root = make_test_root("memory_writeback_blank")
        runner = FakeCodexRunner()
        service = self.build_service(root, runner=runner, auto=True, memory_writeback_prompt="")

        service._run_memory_writeback_worker(123, root, "user is building a long-term bot memory system")

        self.assertEqual(service.memory_store.list_memories(123), [])
        self.assertEqual(runner.calls, [])

    def test_humanize_memory_text_rewrites_user_style_phrasing(self) -> None:
        root = make_test_root("memory_humanize")
        service = self.build_service(root)

        self.assertEqual(
            service._humanize_memory_text('对方会称呼我“昵称”'),
            "NN会叫我“昵称”。",
        )
        self.assertEqual(
            service._humanize_memory_text("对方出生于1998年4月9日，生日是1998年4月9日"),
            "NN出生于1998年4月9日。",
        )


if __name__ == "__main__":
    unittest.main()
