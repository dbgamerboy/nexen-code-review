"""Catalogue shape regressions on H: fixtures; original image folders are untouched."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import wdr_lookbook as m


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='H:/NEXEN/temp');self.root=Path(self.temp.name)
        self.images=self.root/'images';self.images.mkdir();self.index=self.root/'index.json'
        self.raw=b'\x89PNG\r\n\x1a\nfixture-image'
        (self.images/'one.png').write_bytes(self.raw);(self.images/'copy.png').write_bytes(self.raw)
        self.valid=m.build_catalog(self.images,self.index,observations={})
    def tearDown(self):self.temp.cleanup()
    def save(self,value):self.index.write_text(json.dumps(value),encoding='utf-8')
    def invalid(self,value):
        self.save(value)
        with self.assertRaises(HTTPException) as error:m.load_index(self.index)
        self.assertEqual(error.exception.status_code,503)
    def test_valid_catalogue_and_all_duplicate_sources_are_preserved(self):
        self.assertEqual(m.load_index(self.index),self.valid)
        self.assertEqual(len(m.load_index(self.index)['assets'][0]['sources']),2)
        self.assertEqual(m.image_bytes(hashlib.sha256(self.raw).hexdigest(),self.images,self.index),(self.raw,'image/png'))
    def test_missing_consumed_catalogue_fields_return_controlled_503(self):
        for key in ('schema','indexed_at','source_label','scope','counts','assets'):
            with self.subTest(key=key):
                value=copy.deepcopy(self.valid);del value[key];self.invalid(value)
        for patch_value in ({'assets':[None]},{'assets':['old-format']},{'counts':[]},{'counts':{'unique_assets':'347'}},{'indexed_at':None}):
            with self.subTest(patch=patch_value):self.invalid({**self.valid,**patch_value})
    def test_bad_asset_types_paths_and_sources_return_controlled_503(self):
        malformed=[{'id':None},{'id':'../bad'},{'sha256':'b'*64},{'title':None},{'caption':[]},
                   {'role':{}},{'tags':[None]},{'tags':'tag'},{'sources':None},{'sources':[]},
                   {'sources':[None]},{'sources':[{}]},{'bytes':True},{'mime':[]},
                   {'review_status':None},{'reviewed_at':{}},
                   {'sources':[{**self.valid['assets'][0]['sources'][0],'relative_path':'../outside.png'}]}]
        for change in malformed:
            with self.subTest(change=change):
                value=copy.deepcopy(self.valid);value['assets'][0].update(change);self.invalid(value)
    def test_http_search_and_image_both_fail_cleanly_for_malformed_catalogue(self):
        value=copy.deepcopy(self.valid);value['assets'][0]['tags']=[{}];self.save(value)
        app=FastAPI();m.register(app);original=m.load_index
        with patch.object(m,'load_index',side_effect=lambda *args,**kwargs:original(self.index)):
            client=TestClient(app)
            self.assertEqual(client.get('/api/wdr/assets?q=anything').status_code,503)
            self.assertEqual(client.get('/api/wdr/assets/'+self.valid['assets'][0]['id']+'/image').status_code,503)
        self.assertEqual((self.images/'one.png').read_bytes(),self.raw)


if __name__=='__main__':unittest.main()
