"""CLI parser control: no prior-round defaults or trial data opened."""
import subprocess,sys,unittest
from pathlib import Path
B=Path(__file__).resolve().parent
class ExplicitPaths(unittest.TestCase):
 def test_run_is_mandatory(self):
  r=subprocess.run([sys.executable,str(B/'audit-final-export.py'),'--base','/private/tmp/synthetic-not-real','--formal-release-confirmed','--complete-export-confirmed','--binding-json','/private/tmp/synthetic-not-real.json','--output','/private/tmp/not-written.json'],capture_output=True,text=True);self.assertEqual(r.returncode,2);self.assertIn('--run',r.stderr);self.assertNotIn('Traceback',r.stderr)
if __name__=='__main__':unittest.main()
