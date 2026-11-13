"""SQLite positional inverted index and explainable BM25 retrieval."""
from collections import defaultdict
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata

WORD = re.compile(r'[^\W_]+', re.UNICODE)


def tokens(text):
    return [(m.group().casefold(), m.start(), m.end()) for m in WORD.finditer(text)]


def document(data):
    if not isinstance(data, dict) or set(data) != {'title', 'text'}:
        raise ValueError('Each document needs title and text only.')
    result = {}
    for key, limit in [('title', 120), ('text', 40000)]:
        value = data[key]
        if not isinstance(value, str) or len(value) > limit or '\x00' in value:
            raise ValueError(f'{key} must be text of at most {limit} characters, without NUL.')
        value = unicodedata.normalize('NFKC', value)
        if len(value) > limit:
            raise ValueError(f'Normalized {key} exceeds {limit} characters.')
        result[key] = value
    if not result['title'].strip():
        raise ValueError('A title is required.')
    return result


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('ID and version must be positive integers.')
    return value


class Conflict(ValueError):
    pass


class Library:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, text TEXT NOT NULL,
                    length INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)));
                CREATE TABLE IF NOT EXISTS postings (
                    term TEXT NOT NULL, doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    frequency INTEGER NOT NULL, positions TEXT NOT NULL,
                    PRIMARY KEY(term,doc_id));
                CREATE INDEX IF NOT EXISTS postings_document ON postings(doc_id);
            ''')
        conn.close()

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def index(conn, doc_id, data):
        title, body = tokens(data['title']), tokens(data['text'])
        postings = defaultdict(list)
        for i, (term, _, _) in enumerate(title):
            postings[term].append(i)
        # A missing position separates title and body for phrase matching.
        for i, (term, _, _) in enumerate(body, len(title) + 1):
            postings[term].append(i)
        conn.execute('DELETE FROM postings WHERE doc_id=?', (doc_id,))
        conn.executemany('INSERT INTO postings VALUES(?,?,?,?)',
                         [(term, doc_id, len(positions), json.dumps(positions)) for term, positions in postings.items()])
        conn.execute('UPDATE documents SET length=? WHERE id=?', (len(title) + len(body), doc_id))

    @staticmethod
    def capacity(conn, new_count=0, new_chars=0):
        count, chars = conn.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(title)+LENGTH(text)),0) FROM documents').fetchone()
        if count + new_count > 500 or chars + new_chars > 2000000:
            raise ValueError('Library limit: 500 documents and 2,000,000 characters including archived documents.')

    def add_many(self, documents):
        if not isinstance(documents, list) or not 1 <= len(documents) <= 500:
            raise ValueError('Import 1–500 documents at a time.')
        normalized = [document(item) for item in documents]
        with self.connection(write=True) as conn:
            self.capacity(conn, len(normalized), sum(len(d['title'])+len(d['text']) for d in normalized))
            ids = []
            for data in normalized:
                cursor = conn.execute('INSERT INTO documents(title,text,length) VALUES(?,?,0)', (data['title'], data['text']))
                ids.append(cursor.lastrowid)
                self.index(conn, cursor.lastrowid, data)
            return ids

    def update(self, doc_id, version, data):
        doc_id, version, data = positive_id(doc_id), positive_id(version), document(data)
        with self.connection(write=True) as conn:
            previous = conn.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
            if previous is None or previous['version'] != version:
                raise Conflict('Document changed or no longer exists. Reload before saving.')
            self.capacity(conn, new_chars=len(data['title'])+len(data['text'])-len(previous['title'])-len(previous['text']))
            conn.execute('UPDATE documents SET title=?,text=?,version=version+1 WHERE id=?', (data['title'],data['text'],doc_id))
            self.index(conn, doc_id, data)
            return version + 1

    def archive(self, doc_id, version, archived):
        positive_id(doc_id)
        positive_id(version)
        if type(archived) is not bool:
            raise ValueError('archived must be a boolean.')
        with self.connection(write=True) as conn:
            cursor = conn.execute('UPDATE documents SET archived=?,version=version+1 WHERE id=? AND version=?', (int(archived),doc_id,version))
            if cursor.rowcount != 1:
                raise Conflict('Document changed or no longer exists. Reload before updating.')
            return version + 1

    def get(self, doc_id):
        positive_id(doc_id)
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
            if row is None:
                raise ValueError('Document not found.')
            return dict(row)

    def listing(self):
        with self.connection() as conn:
            docs = [dict(row) for row in conn.execute('SELECT id,title,length,version,archived FROM documents ORDER BY id DESC')]
            terms = conn.execute('SELECT COUNT(DISTINCT term) FROM postings p JOIN documents d ON d.id=p.doc_id WHERE d.archived=0').fetchone()[0]
            active = [d for d in docs if not d['archived']]
            return {'documents': docs, 'active': len(active), 'terms': terms, 'tokens': sum(d['length'] for d in active)}

    def export(self):
        with self.connection() as conn:
            return {'documents': [dict(row) for row in conn.execute('SELECT title,text FROM documents WHERE archived=0 ORDER BY id')]}

    def search(self, query, mode='any', k1=1.2, b=0.75, limit=30):
        if not isinstance(query, str) or len(query) > 500 or mode not in ('any', 'all', 'phrase'):
            raise ValueError('Use a query up to 500 characters and any, all, or phrase mode.')
        for name, value, low, high in [('k1',k1,0,3),('b',b,0,1)]:
            if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name} must be a finite number from {low} to {high}.')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('Result limit must be 1–50.')
        ordered = [t[0] for t in tokens(unicodedata.normalize('NFKC',query))]
        if len(ordered) > 32:
            raise ValueError('Use at most 32 query tokens.')
        terms = list(dict.fromkeys(ordered))
        with self.connection() as conn:
            count, average = conn.execute('SELECT COUNT(*),COALESCE(AVG(length),0) FROM documents WHERE archived=0').fetchone()
            postings, frequencies, candidates = {}, {}, set()
            for term in terms:
                rows = conn.execute('SELECT p.* FROM postings p JOIN documents d ON p.doc_id=d.id WHERE term=? AND d.archived=0', (term,)).fetchall()
                postings[term] = {r['doc_id']: {'frequency':r['frequency'], 'positions':set(json.loads(r['positions']))} for r in rows}
                frequencies[term] = len(rows)
                candidates.update(postings[term])
            results = []
            for doc_id in candidates:
                if mode in ('all','phrase') and not all(doc_id in postings[t] for t in terms):
                    continue
                if mode == 'phrase' and not any(all(start+offset in postings[term][doc_id]['positions'] for offset,term in enumerate(ordered)) for start in postings[ordered[0]][doc_id]['positions']):
                    continue
                doc = dict(conn.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone())
                explanation = []
                for term in terms:
                    tf = postings[term].get(doc_id, {}).get('frequency',0)
                    df = frequencies[term]
                    idf = math.log1p((count-df+0.5)/(df+0.5))
                    contribution = idf * tf*(k1+1)/(tf+k1*(1-b+b*doc['length']/(average or 1))) if tf else 0
                    explanation.append({'term':term,'tf':tf,'df':df,'idf':idf,'contribution':contribution})
                combined = doc['title']+'\n'+doc['text']
                matches = [(start,end) for term,start,end in tokens(combined) if term in terms]
                anchor = matches[0][0] if matches else 0
                start, end = max(0,anchor-60), min(len(combined),max(0,anchor-60)+260)
                segments, cursor = [], start
                for a,z in matches:
                    if a < start or z > end:
                        continue
                    if cursor<a:
                        segments.append({'text':combined[cursor:a],'match':False})
                    segments.append({'text':combined[a:z],'match':True})
                    cursor=z
                segments.append({'text':combined[cursor:end],'match':False})
                results.append({'id':doc_id,'title':doc['title'],'length':doc['length'],
                                'score':sum(e['contribution'] for e in explanation),'explanation':explanation,
                                'snippet':segments,'leading_ellipsis':start>0,'trailing_ellipsis':end<len(combined)})
            results.sort(key=lambda d:(-d['score'],d['id']))
            return {'results':results[:limit], 'total':len(results), 'corpus_size':count,
                    'average_length':average,'query_terms':terms,'mode':mode,'k1':k1,'b':b}


SAMPLES = [
    {'title':'A field guide to inverted indexes','text':'An inverted index maps each term to a posting list of documents. Positional postings record where each token occurs. They support phrase queries without scanning every document. Updating a document must replace its old postings atomically.'},
    {'title':'Ranking with BM25','text':'BM25 combines inverse document frequency, term frequency saturation, and document length normalization. Rare terms carry more weight. Repeating a word helps less each time. The b parameter controls the length adjustment; k1 controls saturation. Scores describe relevance to the current corpus, not probabilities.'},
    {'title':'Transactions and durable writes','text':'SQLite transactions keep document content and its search index consistent. A write transaction updates both together. Write-ahead logging allows readers to continue while a writer prepares its transaction. Optimistic version checks prevent an outdated editor from overwriting newer text.'},
    {'title':'Phrase matching in a library','text':'A phrase query asks for adjacent tokens in order. The query distributed systems differs from systems distributed. Token positions let the search engine check adjacency. Punctuation and case may be normalized, depending on the tokenizer.'},
    {'title':'Notes on distributed systems','text':'Distributed systems use replication, consensus, and logical clocks to coordinate across machines. A network partition prevents some messages from arriving. A search index can be partitioned across shards, but merging ranked results requires consistent corpus statistics.'},
    {'title':'The precision and recall tradeoff','text':'Precision measures how many retrieved documents are relevant. Recall measures how many relevant documents were retrieved. Relevance judgments are needed to evaluate these metrics. A high BM25 score alone does not prove that a result answers a question.'}
]
