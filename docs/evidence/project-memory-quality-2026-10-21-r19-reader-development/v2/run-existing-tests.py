from pathlib import Path
import importlib.util
import sys
import unittest
ROOT=Path('/Users/alexandrbasis/Desktop/Coding/codex-mem-public');sys.path.insert(0,str(ROOT))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
helper=load('prospective_owned_helper',Path(__file__).with_name('helper.py'));helper.ROOT=ROOT
suite=load('existing_protocol_suite',ROOT/'scripts/consolidation_experiment/test_protocol.py')
suite.experiment=helper
# Existing tests use SCRIPT both as repository fixture locator and held-helper digest.
class ScriptBinding:
    parents=[ROOT/'scripts',ROOT]
    def read_bytes(self):return Path(__file__).with_name('helper.py').read_bytes()
suite.SCRIPT=ScriptBinding()
r=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(suite));sys.exit(not r.wasSuccessful())
