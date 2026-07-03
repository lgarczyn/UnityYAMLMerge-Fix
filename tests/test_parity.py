"""The C# port running on Unity's Mono must match the Python reference
byte-for-byte: same merge output, same exit code, same guard messages.
Skipped when no Unity install provides mono, as on a bare CI runner."""
import os
import subprocess

import pytest

from conftest import HDR, RID, ROOT, TAKE_THEIRS, asset, drop_entry, entry, refrec, table
from test_reserialize import SCALARS, rewrap, unwrap


def run_driver(cmd, tool, workdir, base, theirs, ours):
    workdir.mkdir()
    paths = []
    for name, text in zip(("base", "remote", "local", "out"), (base, theirs, ours, ours)):
        p = workdir / name
        p.write_bytes(text.encode())
        paths.append(str(p))
    r = subprocess.run(cmd + paths, capture_output=True, text=True,
                       env=dict(os.environ, UNITY_YAML_MERGE=tool))
    return r.returncode, (workdir / "out").read_bytes(), r.stderr


MERGE_CASES = [
    ("clean", TAKE_THEIRS,
     lambda: (table((100, "a"), (200, "old")),
              table((100, "a"), (200, "new")),
              table((100, "a"), (200, "old")))),
    ("recovered-drop", drop_entry(100),
     lambda: (table((100, "victim"), (200, "old")),
              table((100, "victim"), (200, "new")),
              table((100, "victim"), (200, "old")))),
    ("conflict-drop", drop_entry(100),
     lambda: (table((100, "victim"), (200, "old")),
              table((100, "victim"), (200, "theirs version")),
              table((100, "victim"), (200, "ours version")))),
    ("smart-flag-strip",
     "text = open(remote).read().replace("
     "'      m_Items:\\n      - rid: " + RID + "', '      m_Items: []')\n"
     "open(out, 'w').write(text)",
     lambda: (asset([entry(100, "'{s}'", [RID])], [refrec(RID, [100])]),) * 3),
    ("side-pick", "shutil.copyfile(local, out)",
     lambda: (table((100, "original")), table((100, "THEIRS")), table((100, "OURS")))),
]


class TestMergeParity:

    @pytest.mark.parametrize("name,tool_body,files", MERGE_CASES,
                             ids=[c[0] for c in MERGE_CASES])
    def test_same_output_rc_and_stderr(self, csharp, fake_tool, tmp_path,
                                       name, tool_body, files):
        mono, exe = csharp
        tool = fake_tool(tool_body)
        base, theirs, ours = files()
        py = run_driver(["python3", str(ROOT / "unityyamlmerge_fix.py")],
                        tool, tmp_path / "py", base, theirs, ours)
        cs = run_driver([mono, exe], tool, tmp_path / "cs", base, theirs, ours)
        assert py == cs


class TestCodecParity:

    def test_batch_modes_match_reference(self, csharp, tmp_path):
        mono, exe = csharp
        docs = tmp_path / "docs"
        docs.mkdir()
        paths = []
        for i, param in enumerate(SCALARS):
            p = docs / str(i)
            p.write_bytes((HDR + param.values[0] + "\n").encode())
            paths.append(str(p))
        listfile = tmp_path / "list.txt"
        listfile.write_text("\n".join(paths) + "\n")
        for mode, ref in (("--batch-reserialize", rewrap), ("--batch-unwrap", unwrap)):
            outdir = tmp_path / mode.strip("-")
            subprocess.run([mono, exe, mode, str(listfile), str(outdir)], check=True)
            for i, p in enumerate(paths):
                got = (outdir / str(i)).read_bytes()
                want = ref(open(p, newline="").read()).encode()
                assert got == want, (mode, i)
