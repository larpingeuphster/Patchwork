import xml.etree.ElementTree as ET

from patchwork import startup

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_task_xml_runs_elevated_at_logon_without_limits():
    xml = startup.task_xml(r"C:\Users\Kiran & Co\Downloads\Patchwork-v1.0.5-Windows.exe", r"PC\Kiran")
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == r"PC\Kiran"
    assert root.find("t:Principals/t:Principal/t:RunLevel", NS).text == "HighestAvailable"
    assert root.find("t:Settings/t:DisallowStartIfOnBatteries", NS).text == "false"
    assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
    assert root.find("t:Actions/t:Exec/t:Command", NS).text.endswith("Patchwork-v1.0.5-Windows.exe")
    assert "Kiran & Co" in root.find("t:Actions/t:Exec/t:Command", NS).text  # escaped correctly
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == startup.BACKGROUND_FLAG


def test_not_supported_outside_the_packaged_app():
    assert not startup.supported()
    assert startup.enable() is not None
    assert startup.disable() is None
    assert startup.is_enabled() is False
