import json
import pytest
import yaml
from bench import db, export
from bench.scheduler import Bench
from bench.web import create_app


def setup(bench_env):
    b=Bench(str(bench_env))
    b.sync_models(force=True)
    matches=db.rows('SELECT * FROM matches ORDER BY seq')
    target='random-two'
    chosen=next(m for m in matches if target in (m['home_model'],m['away_model']))
    unaffected=next(m for m in matches if target not in (m['home_model'],m['away_model']))
    meta=json.dumps({'harness':{'protocol_version':'1.7'},'original':'kept'})
    for i,m in enumerate((chosen,unaffected)):
        db.execute("UPDATE matches SET status='completed',home_score=2,away_score=0,winner='home',finished_at=?,meta=?,home_stats='{}',away_stats='{}' WHERE id=?",(100+i,meta,m['id']))
    return b,target,chosen,unaffected


def test_archive_preserves_evidence_and_is_idempotent(bench_env):
    b,mid,m,other=setup(bench_env)
    original=db.row('SELECT * FROM matches WHERE id=?',(m['id'],))
    queued=db.rows("SELECT id FROM matches WHERE status='queued' AND (home_model=? OR away_model=?)",(mid,mid))
    db.archive_condition(mid,'Serving-path failure')
    row=db.decode_match(db.row('SELECT * FROM matches WHERE id=?',(m['id'],)))
    assert row['status']=='archived'
    assert row['meta']['archive']['reason']=='Serving-path failure'
    assert row['meta']['archive']['previous_status']=='completed'
    assert row['meta']['original']=='kept'
    for k in ('home_score','away_score','winner','home_stats','away_stats','finished_at','seed','frames'):
        assert db.row('SELECT * FROM matches WHERE id=?',(m['id'],))[k]==original[k]
    assert all(db.row('SELECT status FROM matches WHERE id=?',(q['id'],))['status']=='cancelled' for q in queued)
    assert db.row('SELECT status FROM matches WHERE id=?',(other['id'],))['status']=='completed'
    snapshot=db.rows('SELECT * FROM matches ORDER BY id')
    db.archive_condition(mid,'Serving-path failure')
    assert db.rows('SELECT * FROM matches ORDER BY id')==snapshot


def test_archive_refuses_live_game_without_partial_mutation(bench_env):
    b,mid,m,other=setup(bench_env)
    q=db.row("SELECT id FROM matches WHERE status='queued' AND (home_model=? OR away_model=?)",(mid,mid))
    db.execute("UPDATE matches SET status='running' WHERE id=?",(q['id'],))
    before=db.rows('SELECT * FROM matches ORDER BY id')
    with pytest.raises(ValueError,match='running'):
        db.archive_condition(mid,'Serving-path failure')
    assert db.rows('SELECT * FROM matches ORDER BY id')==before


def test_config_archive_retires_condition_and_excludes_both_sides(bench_env):
    b,mid,m,other=setup(bench_env)
    cfg=yaml.safe_load(bench_env.read_text())
    for x in cfg['models']:
        if x['name']=='Random two':x.update(enabled=False,archive_reason='Serving-path failure')
    bench_env.write_text(yaml.safe_dump(cfg))
    b.sync_models(force=True)
    assert db.row('SELECT status FROM matches WHERE id=?',(m['id'],))['status']=='archived'
    assert not b.models[mid]['enabled']
    app=create_app(start_scheduler=False); c=app.test_client()
    lb=c.get('/api/leaderboard').get_json()
    assert mid not in [x['id'] for x in lb['models']]
    assert sum(x['played'] for x in lb['models'])==2
    assert lb['matches']==1
    assert c.get('/api/models/'+mid).get_json()['stats'] is None
    assert m['id'] not in [x['id'] for x in c.get('/api/matches').get_json()['matches']]
    archived=c.get('/api/matches?archived=1').get_json()['matches']
    assert [x['id'] for x in archived]==[m['id']]
    assert archived[0]['archive']['reason']=='Serving-path failure'
    assert c.get('/api/matches/'+m['id']).get_json()['status']=='archived'
    import os
    os.environ['ADMIN_TOKEN']='archive-test-token'
    try:
        cancelled=db.row("SELECT id FROM matches WHERE status='cancelled' AND (home_model=? OR away_model=?)",(mid,mid))
        assert c.post('/api/admin/requeue/'+cancelled['id'],headers={'Authorization':'Bearer archive-test-token'}).status_code==400
    finally:
        os.environ.pop('ADMIN_TOKEN',None)
    assert m['id'] in [x['id'] for x in export.iter_matches()]
    html=c.get('/matches').get_data(as_text=True)
    assert 'Archived only' in html
    match_html=c.get('/match/'+m['id']).get_data(as_text=True)
    assert 'Excluded from ratings' in match_html


def test_archive_requires_disabled_condition(bench_env):
    cfg=yaml.safe_load(bench_env.read_text())
    cfg['models'][0]['archive_reason']='Serving-path failure'
    bench_env.write_text(yaml.safe_dump(cfg))
    from bench.config import load_models_file
    with pytest.raises(ValueError,match='disabled'):
        load_models_file(str(bench_env))
