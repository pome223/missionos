"""Check the English edition against the Japanese source and shared evidence."""
from pathlib import Path
import json,re,hashlib
R=Path(__file__).resolve().parent
ja=(R/'report.md').read_text();en=(R/'report-en.md').read_text()
# Numerical entries in the measurements and timelines must remain identical.
def table_numbers(s):
 return [re.findall(r'(?<![A-Za-z_])\d+(?:[.,]\d+)*',line) for line in s.splitlines() if line.startswith('|')]
assert table_numbers(ja)==table_numbers(en),'Numerical table mismatch'
assert len(re.findall(r'^## ',en,re.M))==12
pages={lang:(R/f).read_text() for lang,f in [('ja','index.html'),('en','index-en.html')]}
raw=json.loads((R/'data/replay.json').read_text())
for lang,page in pages.items():
 assert f'lang="{lang}"' in page
 assert json.loads(re.search(r'<script type="application/json" id="replay-data">(.*?)</script>',page,re.S).group(1))==raw
 assert page.count('<video ')==2
 for link in re.findall(r'(?:href|src|poster)="([^"]+)"',page):
  if not link.startswith(('http://','https://','#')):assert (R/link).exists(),link
 assert 'fetch(' not in page
visible_en=re.sub(r'<script.*?</script>','',pages['en'],flags=re.S)
assert '再生' not in visible_en and 'シミュレーション時刻' not in visible_en
for literal in ['cf3db6c','2,715','76','87.390','95.455','5.259115','1.090103','Go2で会議室Aへ届けて']:
 assert literal in en,literal
print(json.dumps({'status':'passed','sections':12,'videos':2,'numeric_tables_identical':True,'embedded_datasets_identical':True,'local_links_resolve':True,'tested_request_preserved':True,'english_source_words':len(en.split())},indent=2))
