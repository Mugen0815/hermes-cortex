import json
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / 'scripts' / 'promote_session.py'


class PromoteSessionScriptTests(unittest.TestCase):
    def make_session(self, directory: Path, name: str, rows: list[dict]) -> Path:
        path = directory / name
        with path.open('w', encoding='utf-8') as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        return path

    def run_script(self, session_path: Path, vault_root: Path, extra_args: list[str] | None = None):
        command = ['python3', str(SCRIPT), str(session_path), '--vault-root', str(vault_root)]
        if extra_args:
            command.extend(extra_args)
        return subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True)

    def test_generates_review_digest_with_memory_vault_and_repo_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault_root = tmp / 'vault'
            session_path = self.make_session(
                tmp,
                '20260425_200627_dc1a84.jsonl',
                [
                    {'role': 'session_meta', 'tools': []},
                    {'role': 'user', 'content': 'Mir ist wichtig, dass du systematisch über Workflows und Skills statt Intuition arbeitest.'},
                    {'role': 'assistant', 'content': 'Wir dokumentieren die Workspace-Struktur unter /srv/example-homebase/docs/workspace-layout.md.'},
                    {'role': 'assistant', 'content': 'Hermes Gateway läuft als user-systemd service auf dieser VM.'},
                ],
            )

            result = self.run_script(session_path, vault_root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('WROTE', result.stdout)

            output_file = vault_root / '00_inbox' / 'Session Promotion - 20260425_200627_dc1a84.md'
            self.assertTrue(output_file.exists())
            output = output_file.read_text(encoding='utf-8')
            self.assertIn('# Session Promotion - 20260425_200627_dc1a84', output)
            self.assertIn('status: draft', output)
            self.assertIn('review_status: pending', output)
            self.assertIn('review_reason:', output)
            self.assertNotIn('status: review', output.split('---', 2)[1])
            self.assertIn('## Memory candidates', output)
            self.assertIn('## Vault candidates', output)
            self.assertIn('## Repo candidates', output)
            self.assertIn('systematisch über Workflows und Skills', output)
            self.assertIn('/srv/example-homebase/docs/workspace-layout.md', output)
            self.assertIn('Hermes Gateway läuft als user-systemd service', output)

    def test_stdout_mode_prints_digest_without_writing_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault_root = tmp / 'vault'
            session_path = self.make_session(
                tmp,
                '20260425_210000_abcdef12.jsonl',
                [
                    {'role': 'user', 'content': 'Bitte speichere nur dauerhafte Fakten.'},
                    {'role': 'assistant', 'content': 'Temporäre Diskussionen bleiben in Sessions.'},
                ],
            )

            result = self.run_script(session_path, vault_root, ['--stdout'])

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('# Session Promotion - 20260425_210000_abcdef12', result.stdout)
            self.assertFalse((vault_root / '00_inbox').exists())

    def test_redacts_secret_like_content_from_digest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault_root = tmp / 'vault'
            session_path = self.make_session(
                tmp,
                '20260425_220000_deadbeef.jsonl',
                [
                    {'role': 'user', 'content': 'Bitte merk dir: das production token ist sk-live-super-secret-value und bleibt wichtig.'},
                ],
            )

            result = self.run_script(session_path, vault_root)

            self.assertEqual(result.returncode, 0, result.stderr)
            output_file = vault_root / '00_inbox' / 'Session Promotion - 20260425_220000_deadbeef.md'
            output = output_file.read_text(encoding='utf-8')
            self.assertNotIn('sk-live-super-secret-value', output)
            self.assertIn('[REDACTED]', output)

    def test_returns_clean_error_for_invalid_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault_root = tmp / 'vault'
            session_path = tmp / 'broken.jsonl'
            session_path.write_text('{not-valid-json}\n', encoding='utf-8')

            result = self.run_script(session_path, vault_root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn('ERROR invalid session log', result.stderr)
            self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
