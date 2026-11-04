"""FTPS verifies the server certificate (review 2026-10-22). CERT_NONE skipped
the chain as well as the name, so anyone on the path could collect the FTP
passwords and feed fake files. Now the chain is always verified; the name is
checked against FTPS_TLS_SERVER_NAME when it is set."""
import ssl
import sys

sys.path.insert(0, ".")

from integrations import sftp_inbox


class _FakeFTP:
    made = []

    def __init__(self, context=None, timeout=None):
        self.context, self.host, self.calls = context, "", []
        _FakeFTP.made.append(self)

    def connect(self, host, port):
        self.host = host
        self.calls.append(("connect", host, port))

    def auth(self):
        self.calls.append(("auth", self.host))

    def login(self, user, passwd):
        self.calls.append(("login", user))

    def prot_p(self):
        self.calls.append(("prot_p",))


def test_chain_always_verified_name_only_when_configured(monkeypatch):
    monkeypatch.setattr(sftp_inbox.ftplib, "FTP_TLS", _FakeFTP)
    monkeypatch.delenv("FTPS_TLS_SERVER_NAME", raising=False)
    ftp = sftp_inbox._connect("sftp.hhbagels.com", 21, "u", "p")
    assert ftp.context.verify_mode == ssl.CERT_REQUIRED
    assert ftp.context.check_hostname is False
    assert ("auth", "sftp.hhbagels.com") in ftp.calls

    monkeypatch.setenv("FTPS_TLS_SERVER_NAME", "p3plcpnl0001.prod.phx3.secureserver.net")
    ftp = sftp_inbox._connect("sftp.hhbagels.com", 21, "u", "p")
    assert ftp.context.verify_mode == ssl.CERT_REQUIRED
    assert ftp.context.check_hostname is True
    # TCP still goes to the configured host; TLS (SNI + name check) uses the
    # certificate's name.
    assert ftp.calls[0] == ("connect", "sftp.hhbagels.com", 21)
    assert ("auth", "p3plcpnl0001.prod.phx3.secureserver.net") in ftp.calls
