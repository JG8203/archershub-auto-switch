import unittest

from archershub.bot.messages import delete_message_safely


class FakeMessage:
    def __init__(self, should_fail=False):
        self.deleted = False
        self.should_fail = should_fail

    async def delete(self):
        if self.should_fail:
            raise RuntimeError("no permission")
        self.deleted = True


class DeleteMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_message_safely(self):
        msg = FakeMessage()
        self.assertTrue(await delete_message_safely(msg))
        self.assertTrue(msg.deleted)
        self.assertFalse(await delete_message_safely(FakeMessage(should_fail=True)))
        self.assertFalse(await delete_message_safely(None))


if __name__ == "__main__":
    unittest.main()
