"""One shared example of what the jobs write, for the pages' tests (R13): the role tests compare the status they
produce in named cases with tests/fixtures/osoui-status.json, and ui/scripts/tests/openstack-access.test.mjs renders
the pages from the same file. A change of the jobs' output fails the role test until the file is written again
(SHC_WRITE_FIXTURES=1 writes it), and then the page test runs on the new shape.

Ids the fakes make up are replaced by fixed ones, so the file does not change from run to run."""
import json
import os

PATH = os.path.join(os.path.dirname(__file__), "..", "fixtures", "osoui-status.json")


def _normal(value, ids):
    text = json.dumps(value, sort_keys=True)
    for real, fixed in ids.items():
        if real:
            text = text.replace(real, fixed)
    return json.loads(text)


def check(test, name, value, ids):
    """Compare value (ids replaced) with the file's entry `name`; with SHC_WRITE_FIXTURES=1 write it instead."""
    value = _normal(value, ids)
    data = {}
    if os.path.exists(PATH):
        with open(PATH) as f:
            data = json.load(f)
    if os.environ.get("SHC_WRITE_FIXTURES") == "1":
        data[name] = value
        with open(PATH, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.write("\n")
        return
    test.assertTrue(name in data, f"{PATH} has no case {name}: run the role tests with SHC_WRITE_FIXTURES=1")
    test.assertEqual(value, data[name], f"the jobs' output for {name} differs from {PATH}: write it again "
                                        "(SHC_WRITE_FIXTURES=1) and let the page tests run on it")
