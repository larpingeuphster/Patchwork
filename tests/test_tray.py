from patchwork import tray


def test_close_hides_to_tray_and_announces_once():
    hidden, announced = [], []
    closer = tray.CloseToTray(hide=lambda: hidden.append(1), enabled=lambda: True,
                              on_first_hide=lambda: announced.append(1))
    assert closer.on_closing() is False  # stays running
    assert closer.on_closing() is False
    assert len(hidden) == 2 and len(announced) == 1


def test_close_quits_when_tray_is_off_or_quitting():
    hidden = []
    off = tray.CloseToTray(hide=lambda: hidden.append(1), enabled=lambda: False)
    assert off.on_closing() is True
    on = tray.CloseToTray(hide=lambda: hidden.append(1), enabled=lambda: True)
    on.quitting = True
    assert on.on_closing() is True
    assert hidden == []


def test_tray_icon_file_exists():
    assert tray.ICON_PATH.is_file()
