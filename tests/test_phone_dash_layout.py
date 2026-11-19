"""The phone feature is a core dashboard module, not a page-shell exception."""
from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_phone_dashboard_uses_the_core_feature_contract_and_touch_sized_controls():
    script = (ROOT / "agent_companion/server/static/phone.js").read_text()
    css = (ROOT / "agent_companion/server/static/base.css").read_text()
    assert "CC.register('phone'" in script
    assert "CC.get('/api/phone/jobs')" in script
    assert "data-stop" in script and "data-action" in script
    assert ".phone-live" in css and "min-height:44px" in css
