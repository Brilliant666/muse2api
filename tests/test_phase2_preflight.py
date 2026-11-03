"""Pure preflight validation only: no accounts, Store, browser, or network."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
TOOL = ROOT / "tools/phase2_account_preflight.py"


def imported():
    spec = importlib.util.spec_from_file_location("standalone_preflight_test",TOOL)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


preflight = imported()


def metadata(count):
    mapping = [{"account_id":f"legacy-{n}","alias":f"test-{n}","legacy_note":"retained input"} for n in range(count)]
    accounts = [{"id":row["account_id"]} for row in mapping]
    return mapping,accounts


def authenticated_html(principal, user=None):
    values = [{"viewerId":principal,"isAuthenticated":True},
              {"prelude":{"user":{"kind":"user","identifier":user or principal}}}]
    flight = "".join(f"{i+11:x}:"+json.dumps(value)+"\n" for i,value in enumerate(values))
    return "<script>self.__next_f.push("+json.dumps([1,flight])+")</script>"


class Phase2PreflightTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="preflight-metadata-only-")
        self.base = Path(self.scratch.name)
        self.repo, self.private = self.base/"worktree", self.base/"private"
        self.repo.mkdir()
        self.private.mkdir()

    def tearDown(self):
        self.scratch.cleanup()

    def test_import_has_no_config_store_engine_environment_or_network_effects(self):
        environ, path, disabled = dict(os.environ),list(sys.path),logging.root.manager.disable
        with patch.dict(sys.modules,{"config":None,"store":None,"engine":None}):
            with patch("urllib.request.build_opener",side_effect=AssertionError("network prohibited")):
                imported()
        self.assertEqual(dict(os.environ),environ)
        self.assertEqual(sys.path,path)
        self.assertEqual(logging.root.manager.disable,disabled)

    def test_private_path_accepts_explicit_external_directory(self):
        self.assertEqual(preflight.validate_paths(self.repo,self.private),
                         (self.repo.resolve(),self.private.resolve()))
        self.assertEqual(preflight.private_path(self.private,"account-preflight","test-1"),
                         self.private.resolve()/"account-preflight"/"test-1")

    def test_paths_reject_relative_inside_repo_and_git_worktree(self):
        inside = self.repo/"private"
        inside.mkdir()
        other = self.base/"other-git"
        other.mkdir()
        (other/".git").write_text("gitdir: not-used",encoding="utf-8")
        hidden = other/"private"
        hidden.mkdir()
        for repo,root in [(Path("relative"),self.private),(self.repo,Path("relative")),
                          (self.repo,inside),(self.repo,hidden)]:
            with self.subTest(root=str(root)):
                with self.assertRaises(preflight.PreflightValidationError):
                    preflight.validate_paths(repo,root)

    def test_private_child_path_cannot_escape_or_resolve_to_root(self):
        for parts in [("..","outside"),(str(self.repo),),(".",)]:
            with self.subTest(parts=parts):
                with self.assertRaises(preflight.PreflightValidationError):
                    preflight.private_path(self.private,*parts)

    def test_one_nine_and_ten_accounts_and_legacy_metadata_are_accepted(self):
        for count in (1,9,10):
            mapping,accounts=metadata(count)
            self.assertEqual(preflight.validate_mapping(mapping,accounts),mapping)
            self.assertTrue(all(preflight.credential_revision(a)==1 for a in accounts))

    def test_mapping_rejects_zero_eleven_and_nonlist(self):
        for mapping in [[],metadata(11)[0],{},None]:
            with self.assertRaises(preflight.PreflightValidationError):
                preflight.validate_mapping(mapping)

    def test_mapping_rejects_duplicate_ids_and_casefold_aliases(self):
        mapping,accounts=metadata(2)
        mapping[1]["account_id"]=mapping[0]["account_id"]
        with self.assertRaises(preflight.PreflightValidationError):
            preflight.validate_mapping(mapping)
        mapping,accounts=metadata(2)
        mapping[1]["alias"]=mapping[0]["alias"].upper()
        with self.assertRaises(preflight.PreflightValidationError):
            preflight.validate_mapping(mapping)

    def test_mapping_rejects_path_and_windows_device_aliases(self):
        for alias in ("..","../escape",r"folder\escape","/absolute","C:drive","test.",
                      "CON","nul.json","LPT1", "has space"):
            with self.subTest(alias=alias):
                with self.assertRaises(preflight.PreflightValidationError):
                    preflight.validate_mapping([{"account_id":"legacy-1","alias":alias}])

    def test_store_count_and_id_set_must_match_exactly(self):
        mapping,accounts=metadata(2)
        variants=[accounts[:1],[{"id":"legacy-0"},{"id":"foreign"}],
                  [{"id":"legacy-0"},{"id":"legacy-0"}],{}]
        for rows in variants:
            with self.assertRaises(preflight.PreflightValidationError):
                preflight.validate_mapping(mapping,rows)

    def test_credential_revision_keeps_legacy_default_and_validates_cas(self):
        self.assertEqual(preflight.credential_revision({}),1)
        self.assertEqual(preflight.credential_revision({"credential_revision":"3"}),3)
        for value in (True,None,0,-1,1.2,"bad"):
            with self.assertRaises(preflight.PreflightValidationError):
                preflight.credential_revision({"credential_revision":value})

    def test_identity_requires_real_authenticated_html_and_private_hmac_salt(self):
        salt=b"synthetic-private-round-salt-only!"
        uid,digest=preflight.identity_from_html(authenticated_html("1000000000000001"),salt)
        self.assertEqual(uid,"1000000000000001")
        self.assertNotEqual(digest,hashlib.sha256(uid.encode()).hexdigest())
        from tools.phase2_identity import IdentityUnverified
        for html in ("not authenticated",authenticated_html(uid,user="1000000000000002")):
            with self.assertRaises(IdentityUnverified):
                preflight.identity_from_html(html,salt)
        with self.assertRaises(preflight.PreflightValidationError):
            preflight.identity_from_html(authenticated_html(uid),b"short")

    def test_distinctness_uses_only_verified_principals_not_cookies_or_vms(self):
        salt=b"synthetic-private-round-salt-only!"
        _,same=preflight.identity_from_html(authenticated_html("1000000000000001"),salt)
        _,other=preflight.identity_from_html(authenticated_html("1000000000000002"),salt)
        rows=[{"identity_verified":True,"upstream_identity_sha256":same,"cookies":"fake-A","vm":"fake-1"},
              {"identity_verified":True,"upstream_identity_sha256":same,"cookies":"fake-B","vm":"fake-2"}]
        self.assertFalse(preflight.distinct_identities_verified(rows,2))
        rows[1]["upstream_identity_sha256"]=other
        self.assertTrue(preflight.distinct_identities_verified(rows,2))
        rows[1]["identity_verified"]=False
        self.assertFalse(preflight.distinct_identities_verified(rows,2))
        self.assertFalse(preflight.distinct_identities_verified(rows,9))

    def test_cdp_probe_uses_configured_loopback_port(self):
        self.assertEqual(preflight.cdp_url(SimpleNamespace(cdp_port=19421)),
                         "http://127.0.0.1:19421/json/version")
        for port in (True,0,65536,"19421",None):
            with self.assertRaises(preflight.PreflightValidationError):
                preflight.cdp_url(SimpleNamespace(cdp_port=port))

    def test_runtime_storage_stays_in_private_root_and_site_is_fixed_https(self):
        cfg=SimpleNamespace(**{name:str(self.private/name) for name in
            ("base_dir","data_dir","profile_dir","download_dir","media_dir")},
            site_url="https://muse.ai/",cdp_port=19421)
        preflight.validate_runtime_paths(cfg,self.private)
        cfg.profile_dir=str(self.repo/"production-profile")
        with self.assertRaises(preflight.PreflightValidationError):
            preflight.validate_runtime_paths(cfg,self.private)
        cfg.profile_dir=str(self.private/"profile")
        cfg.site_url="https://muse.ai.evil.test/"
        with self.assertRaises(preflight.PreflightValidationError):
            preflight.validate_runtime_paths(cfg,self.private)

    def test_main_error_output_has_only_safe_alias_type_and_usage_fields(self):
        capture=io.StringIO()
        with patch.object(preflight,"run_preflight",side_effect=RuntimeError("private fixture must not appear")):
            with contextlib.redirect_stdout(capture):
                code=preflight.main([str(self.repo),str(self.private)])
        self.assertEqual(code,2)
        value=json.loads(capture.getvalue())
        self.assertEqual(value,{"alias":None,"error_type":"RuntimeError","weekly_used_pct":None})
        self.assertNotIn("private fixture",capture.getvalue())


if __name__=="__main__":
    unittest.main()
