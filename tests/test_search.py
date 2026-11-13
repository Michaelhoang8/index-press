import math
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from search import Conflict, Library, document


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'library.sqlite3'
        self.library = Library(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_bm25_matches_hand_calculation_and_components_sum(self):
        ids = self.library.add_many([{'title':'one','text':'apple apple pear'}, {'title':'two','text':'pear plum'}])
        answer = self.library.search('apple pear')
        result = next(r for r in answer['results'] if r['id']==ids[0])
        expected = math.log(2)*2*2.2/(2+1.2*(.25+.75*4/3.5)) + math.log(1.2)*2.2/(1+1.2*(.25+.75*4/3.5))
        self.assertAlmostEqual(result['score'], expected)
        self.assertAlmostEqual(sum(e['contribution'] for e in result['explanation']),result['score'])
        self.assertEqual(answer['average_length'],3.5)

    def test_boolean_phrase_repetition_and_title_boundary(self):
        ids=self.library.add_many([{'title':'first','text':'red blue red red'}, {'title':'red','text':'blue only'}, {'title':'third','text':'red green blue'}])
        self.assertEqual(self.library.search('red missing',mode='all')['total'],0)
        self.assertEqual(self.library.search('red missing',mode='any')['total'],3)
        self.assertEqual([r['id'] for r in self.library.search('red blue',mode='phrase')['results']],[ids[0]])
        self.assertEqual([r['id'] for r in self.library.search('red red',mode='phrase')['results']],[ids[0]])
        self.assertEqual(self.library.search('red')['results'],self.library.search('red red')['results'])

    def test_unicode_normalization_casefold_and_safe_snippet_segments(self):
        self.library.add_many([{'title':'Straße','text':'Cafe\u0301 ＡＢＣ <script>alert</script>'}])
        for query in ['STRASSE','café','abc']:
            result=self.library.search(query)['results'][0]
            self.assertTrue(any(s['match'] for s in result['snippet']))
        self.assertIn('<script>', ''.join(s['text'] for s in self.library.search('alert')['results'][0]['snippet']))

    def test_update_replaces_postings_and_persists_on_reopen(self):
        doc_id=self.library.add_many([{'title':'note','text':'obsolete token'}])[0]
        self.library.update(doc_id,1,{'title':'note','text':'replacement'})
        reopened=Library(self.path)
        self.assertEqual(reopened.search('obsolete')['total'],0)
        self.assertEqual(reopened.search('replacement')['total'],1)
        self.assertEqual(reopened.get(doc_id)['version'],2)

    def test_archive_restore_recomputes_corpus_statistics(self):
        ids=self.library.add_many([{'title':'one','text':'term'}, {'title':'two','text':'term repeated repeated'}])
        self.library.archive(ids[1],1,True)
        answer=self.library.search('term')
        self.assertEqual((answer['corpus_size'],answer['total'],answer['average_length']),(1,1,2))
        self.assertEqual(self.library.listing()['active'],1)
        self.library.archive(ids[1],2,False)
        self.assertEqual(self.library.search('term')['total'],2)

    def test_failed_import_rolls_back_every_document_and_posting(self):
        original=Library.index
        calls=[]
        def failing(conn,doc_id,data):
            calls.append(doc_id)
            original(conn,doc_id,data)
            if len(calls)==2: raise RuntimeError('simulated interruption')
        with patch.object(Library,'index',staticmethod(failing)):
            with self.assertRaises(RuntimeError):
                self.library.add_many([{'title':'one','text':'alpha'},{'title':'two','text':'beta'}])
        self.assertEqual(self.library.listing()['documents'],[])
        with self.library.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM postings').fetchone()[0],0)

    def test_concurrent_edits_have_one_winner(self):
        doc_id=self.library.add_many([{'title':'note','text':'before'}])[0]
        barrier=threading.Barrier(2)
        outcomes=[]
        def edit(text):
            barrier.wait()
            try:
                self.library.update(doc_id,1,{'title':'note','text':text})
                outcomes.append('saved')
            except Conflict: outcomes.append('conflict')
        threads=[threading.Thread(target=edit,args=(text,)) for text in ['alpha','beta']]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual(sorted(outcomes),['conflict','saved'])
        self.assertEqual(self.library.get(doc_id)['version'],2)

    def test_empty_queries_limits_and_invalid_values(self):
        self.assertEqual(self.library.search('!!!')['total'],0)
        for kwargs in [{'query':None},{'query':'a '*33},{'query':'a','b':float('nan')},{'query':'a','k1':True},{'query':'a','limit':0},{'query':'a','mode':'unknown'}]:
            with self.assertRaises(ValueError): self.library.search(**kwargs)
        for data in [{'title':'','text':'x'},{'title':'x','text':'x'*40001},{'title':'x','text':None},{'title':'x','text':'','extra':1}]:
            with self.assertRaises(ValueError): document(data)

    def test_export_contains_active_content_and_round_trips(self):
        ids=self.library.add_many([{'title':'active','text':'words'},{'title':'archived','text':'hidden'}])
        self.library.archive(ids[1],1,True)
        exported=self.library.export()
        self.assertEqual(exported,{'documents':[{'title':'active','text':'words'}]})
        self.library.add_many(exported['documents'])
        self.assertEqual(self.library.search('words')['total'],2)

    def test_document_count_limit_is_atomic(self):
        for _ in range(5): self.library.add_many([{'title':'a','text':''} for _ in range(100)])
        with self.assertRaises(ValueError): self.library.add_many([{'title':'overflow','text':'extra'}])
        self.assertEqual(len(self.library.listing()['documents']),500)
        self.assertEqual(self.library.search('extra')['total'],0)


if __name__ == '__main__': unittest.main()
