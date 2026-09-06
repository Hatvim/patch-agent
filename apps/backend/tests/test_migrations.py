from alembic.config import Config
from alembic.script import ScriptDirectory


def test_single_head_revision():
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected single head, got {heads}"
