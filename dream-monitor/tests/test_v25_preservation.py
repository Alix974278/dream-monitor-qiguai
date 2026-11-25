"""v2.5 changes presentation only: all backend code and timed-task UI preserved."""
import hashlib,json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

class V25PreservationTests(unittest.TestCase):
    def test_all_backend_and_timed_task_ui_match_v24_release(self):
        baseline=json.loads((ROOT/'V24_BACKEND_BASELINE.json').read_text(encoding='utf-8'))
        for file,digest in baseline['backend_files'].items():
            with self.subTest(file=file):
                self.assertEqual(hashlib.sha256((ROOT/file).read_bytes()).hexdigest(),digest)
        spec=baseline['timed_ui_section']
        app=(ROOT/'ui/app.js').read_text(encoding='utf-8')
        section=app[app.index(spec['start']):app.index(spec['end'])]
        self.assertEqual(hashlib.sha256(section.encode()).hexdigest(),spec['sha256'])
