import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import db, main


class XingtuExecutionAPITests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, 'DB_PATH', str(Path(tmp) / 'kb.db')))
        self.enterContext(patch.object(main.platform_session_manager, 'busy', return_value=False))
        main.current_running_task.clear()
        main.current_running_task.update(is_running=False, task_id=None)
        self.client = self.enterContext(TestClient(main.app))

    def test_old_card_retry_is_new_xingtu_task_with_platform_receipt(self):
        async def executor(keyword, count, **kwargs):
            self.assertEqual(kwargs['discovery_mode'], 'xingtu')
            db.update_task_status(kwargs['task_id'], 'completed', '星图检索完成', {'platform': 'xingtu'})
            return kwargs['task_id']
        with patch.object(main, 'execute_bd_search_task', new=AsyncMock(side_effect=executor)):
            response = self.client.post('/api/execute', json={
                'category': '食品', 'keyword': '食品', 'discovery_mode': 'hybrid'})
        self.assertEqual(response.status_code, 200)
        task_id = response.json()['task_id']
        self.assertEqual(db.get_task(task_id)['requirements']['discovery_mode'], 'xingtu')
        messages = self.client.get('/api/chat/history').json()['messages']
        self.assertEqual(messages[-1]['meta']['platform'], 'xingtu')


if __name__ == '__main__':
    unittest.main()
