import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from run import Handler
from search import Library


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.library=Library(Path(self.temp.name)/'test.sqlite3')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self,path,data=None,headers=None,raw=None,method=None):
        conn=HTTPConnection(*self.server.server_address,timeout=3)
        conn.request(method or ('POST' if data is not None or raw is not None else 'GET'),path,
                     body=raw if raw is not None else json.dumps(data) if data is not None else None,
                     headers={'Content-Type':'application/json',**(headers or {})})
        response=conn.getresponse()
        body=response.read()
        result=response.status,json.loads(body) if body else None,response.headers
        conn.close()
        return result

    def test_import_search_edit_conflict_archive_restore(self):
        status,body,_=self.request('/api/import',{'documents':[{'title':'Note','text':'a searchable phrase'}]})
        self.assertEqual(status,201)
        doc_id=body['ids'][0]
        params={'query':'searchable phrase','mode':'phrase','k1':1.2,'b':.75,'limit':30}
        self.assertEqual(self.request('/api/search',params)[1]['total'],1)
        save={'id':doc_id,'version':1,'document':{'title':'Note','text':'replacement'}}
        self.assertEqual(self.request('/api/save',save)[0],200)
        self.assertEqual(self.request('/api/save',save)[0],409)
        self.assertEqual(self.request('/api/search',params)[1]['total'],0)
        self.assertEqual(self.request('/api/archive',{'id':doc_id,'version':2,'archived':True})[0],200)
        self.assertEqual(self.request('/api/library')[1]['active'],0)
        self.assertEqual(self.request('/api/archive',{'id':doc_id,'version':3,'archived':False})[0],200)
        self.assertEqual(self.request(f'/api/document?id={doc_id}')[1]['version'],4)

    def test_json_boundaries_and_private_files(self):
        for raw in ['[]','{"documents":[],"documents":[]}','{"x":NaN}','['*1500,'{']:
            self.assertEqual(self.request('/api/import',raw=raw)[0],400)
        for path in ['/.git/config','/search.py','/data/library.sqlite3','/../run.py']:
            self.assertEqual(self.request(path)[0],404)
        self.assertEqual(self.request('/api/library',headers={'Host':'evil.invalid'})[0],400)
        self.assertEqual(self.request('/api/import',{'documents':[]},headers={'Origin':'https://evil.invalid'})[0],403)
        self.assertEqual(self.request('/api/library',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.request('/api/library',method='HEAD')[0],200)

    def test_empty_export_and_invalid_api_fields(self):
        self.assertEqual(self.request('/api/export')[1],{'documents':[]})
        self.assertEqual(self.request('/api/save',{'id':1})[0],400)
        self.assertEqual(self.request('/api/document?id=1&id=2')[0],400)
        self.assertEqual(self.request('/api/search',{'query':'x'})[0],400)
        self.assertEqual(self.request('/api/unknown',{})[0],404)


if __name__=='__main__': unittest.main()
