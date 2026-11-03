import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLAYER = Path(__file__).resolve().parents[1] / "player"
sys.path.insert(0, str(PLAYER))
import umaru_launcher as launcher


def signed(payload):
    """Create an ephemeral test key in a separate PowerShell process."""
    script = r'''
$rsa = [System.Security.Cryptography.RSA]::Create(2048)
try {
    $data = [Convert]::FromBase64String('__PAYLOAD__')
    $signature = $rsa.SignData($data, [System.Security.Cryptography.HashAlgorithmName]::SHA256, [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $key = $rsa.ExportParameters($false)
    [pscustomobject]@{
        modulus = [Convert]::ToBase64String($key.Modulus)
        exponent = [Convert]::ToBase64String($key.Exponent)
        signature = [Convert]::ToBase64String($signature)
    } | ConvertTo-Json -Compress
} finally { $rsa.Dispose() }
'''
    script = script.replace('__PAYLOAD__', base64.b64encode(payload).decode('ascii'))
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


class LauncherAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setup = b"MZ\x00test installer bytes"
        cls.data = {
            "schema": 1,
            "version": "1.2",
            "size": len(cls.setup),
            "sha256": hashlib.sha256(cls.setup).hexdigest(),
            "notes": "Ручное обновление",
        }
        cls.payload = json.dumps(cls.data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cls.key = signed(cls.payload)

    def manifest(self, payload=None, signature=None):
        return json.dumps({
            "payload": base64.b64encode(self.payload if payload is None else payload).decode("ascii"),
            "signature": self.key["signature"] if signature is None else signature,
        }).encode("ascii")

    def parse(self, raw):
        return launcher._parse_manifest(raw, modulus_b64=self.key["modulus"],
                                        exponent_b64=self.key["exponent"])

    def test_signature_and_payload_tampering(self):
        offer = self.parse(self.manifest())
        self.assertEqual((offer.version, offer.size), ("1.2", len(self.setup)))
        changed = self.payload.replace(b'"1.2"', b'"1.3"')
        with self.assertRaises(launcher.LauncherUpdateError):
            self.parse(self.manifest(payload=changed))
        bad_signature = bytearray(base64.b64decode(self.key["signature"]))
        bad_signature[0] ^= 1
        with self.assertRaises(launcher.LauncherUpdateError):
            self.parse(self.manifest(signature=base64.b64encode(bad_signature).decode("ascii")))

    def test_strict_manifest_and_version(self):
        for raw in (b'{}', b'{"payload":"x","signature":"y","url":"evil"}',
                    b'{"payload":"x","payload":"y","signature":"z"}'):
            with self.subTest(raw=raw), self.assertRaises(launcher.LauncherUpdateError):
                self.parse(raw)
        for version in ("1.2/../evil", "01.2", "1.2.3", "1.2-beta", "1.9999999999999999"):
            with self.subTest(version=version), self.assertRaises(launcher.LauncherUpdateError):
                launcher._version(version)

    def test_check_only_newer(self):
        with patch.object(launcher, "_download", return_value=self.manifest()), \
             patch.object(launcher, "_parse_manifest", return_value=self.parse(self.manifest())):
            self.assertIsNone(launcher.check("1.2"))
            self.assertIsNone(launcher.check("1.3"))
            self.assertEqual(launcher.check("1.1").version, "1.2")

    def test_verified_download_and_cleanup(self):
        # Patch only the pinned key; preserve actual signature verification.
        with patch.object(launcher, "PUBLIC_MODULUS_B64", self.key["modulus"]), \
             patch.object(launcher, "PUBLIC_EXPONENT_B64", self.key["exponent"]):
            offer = launcher.parse_manifest(self.manifest())
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
                seen = []

                def fake_download(url, channel, limit, dest=None, expected=None, progress=None):
                    seen.append((url, channel, limit, expected))
                    Path(dest).write_bytes(self.setup)
                    # Simulate the transport's final integrity check.
                    if len(self.setup) != expected["size"] or hashlib.sha256(self.setup).hexdigest() != expected["sha256"]:
                        raise launcher.UpdateError("mismatch")

                with patch.object(launcher, "_download", side_effect=fake_download):
                    final = launcher.download(offer, temp)
                self.assertEqual(final.read_bytes(), self.setup)
                self.assertEqual(launcher.verify_installer(offer, final), final)
                self.assertEqual(seen[0][0],
                                 "https://github.com/kapitanzet3-a11y/UmaRU-Updates/releases/download/launcher-v1.2/UmaRU-Setup.exe")
                self.assertEqual(seen[0][1], launcher.MANIFEST_URL)
                self.assertEqual(seen[0][2], len(self.setup))
                self.assertEqual(seen[0][3]["sha256"], self.data["sha256"])
                with patch.object(launcher, "_download", side_effect=launcher.UpdateError("bad hash")):
                    with self.assertRaises(launcher.LauncherUpdateError):
                        launcher.download(offer, temp)
                self.assertEqual(list(Path(temp).rglob("*.part")), [])
                self.assertEqual(len(list(Path(temp).rglob("*.exe"))), 1)
                final.write_bytes(self.setup + b"tampered")
                with self.assertRaises(launcher.LauncherUpdateError):
                    launcher.verify_installer(offer, final)


if __name__ == "__main__":
    unittest.main()
