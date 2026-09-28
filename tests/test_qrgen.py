import qrgen


def test_normalize_config_removes_empty_awg_assignments_and_line_endings():
    config = (
        "\ufeff[Interface]\r\n"
        "PrivateKey = private\r\n"
        "I1 = <b 0x01>\r\n"
        "I2 =   \r\n"
        "I3=\r\n"
        "\r\n"
        "[Peer]\r\n"
        "PresharedKey = psk\r\n"
    )

    assert qrgen._normalize_config_for_qr(config) == (
        "[Interface]\n"
        "PrivateKey = private\n"
        "I1 = <b 0x01>\n"
        "\n"
        "[Peer]\n"
        "PresharedKey = psk\n"
    )


def test_make_qr_png_encodes_normalized_text(monkeypatch):
    captured = {}

    class FakeQr:
        def save(self, output, **kwargs):
            captured["save_options"] = kwargs
            output.write(b"png")

    class FakeSegno:
        @staticmethod
        def make(text, error):
            captured["text"] = text
            captured["error"] = error
            return FakeQr()

    monkeypatch.setattr(qrgen, "_SEGNO_OK", True)
    monkeypatch.setattr(qrgen, "segno", FakeSegno, raising=False)

    png = qrgen.make_qr_png("[Interface]\nI2 =\nPrivateKey = secret\n[Peer]\n")

    assert png == b"png"
    assert captured["text"] == "[Interface]\nPrivateKey = secret\n[Peer]\n"
    assert captured["error"] == "l"
    assert captured["save_options"]["scale"] == 8
    assert captured["save_options"]["border"] == 4
