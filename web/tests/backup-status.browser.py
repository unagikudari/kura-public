"""Authenticated backup status from local evidence; no backup/restore commands."""
import importlib.util,json,os,tempfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
STATUS=None
def receipt(state='verified',old=False):
 stamp=(datetime.now(timezone.utc)-timedelta(hours=2 if old else 0)).isoformat()
 return {'format':'kura-backup-status-v1','checked_at':stamp,'host':'fixture-host','destination':'/data/kura-recovery',
  'state':state,'credentials':'DO-NOT-EXPOSE','backup':{'snapshot':'20261022T0346','created_at':'2026-10-22T03:00:00+00:00',
  'observed_at':stamp,'verified_at':'2026-10-22T07:00:00+00:00','archive_bytes':165286762019,'media_files':47136,'tables_verified':110,'constraints_verified':983}}
def exercise(context,base,width,phone,evidence):
 page=context.new_page();STATUS.write_text(json.dumps(receipt()))
 page.goto(base+'/settings/backup',wait_until='networkidle')
 region=page.get_by_role('region',name='ライブラリのバックアップ状況')
 expect(region.get_by_role('status')).to_contain_text('復元確認済み')
 expect(region.locator('.status.healthy')).to_have_count(1)
 expect(region).to_contain_text('110テーブル・983制約');expect(region).to_contain_text('バックアップ作成：手動')
 assert 'DO-NOT-EXPOSE' not in page.content()
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 if evidence:page.screenshot(path=str(evidence/f'backup-verified-{width}.png'))
 scheduled=receipt();scheduled['automation']={'enabled':True,'next_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()}
 STATUS.write_text(json.dumps(scheduled));page.reload(wait_until='networkidle')
 expect(region).to_contain_text('毎週日曜 1:15〜1:30');expect(region).to_contain_text('次回実行')
 scheduled['job']={'state':'running','snapshot':'20261024T070000','started_at':datetime.now(timezone.utc).isoformat(),'finished_at':None}
 STATUS.write_text(json.dumps(scheduled));page.reload(wait_until='networkidle')
 expect(region.get_by_role('status')).to_contain_text('バックアップと復元確認を実行中')
 expect(region.locator('.status.healthy')).to_have_count(0)
 scheduled['job']['state']='failed'
 STATUS.write_text(json.dumps(scheduled));page.reload(wait_until='networkidle')
 expect(region.get_by_role('status')).to_contain_text('最新のバックアップ実行に失敗')
 expect(region).to_contain_text('前回のバックアップを保持');expect(region.locator('.status.healthy')).to_have_count(0)
 STATUS.write_text(json.dumps(receipt('unavailable')));page.get_by_role('link',name='状況を更新',exact=True).click()
 expect(region.get_by_role('status')).to_contain_text('保存先の状況を確認できません');expect(region.locator('.status.healthy')).to_have_count(0)
 expect(region).to_contain_text('最後に取得できた記録')
 STATUS.write_text(json.dumps(receipt(old=True)));page.reload()
 expect(region.get_by_role('status')).to_contain_text('状況の更新を確認できていません');expect(region.locator('.status.healthy')).to_have_count(0)
 STATUS.write_text('invalid');page.reload();expect(region.get_by_role('status')).to_contain_text('バックアップ状況は未確認')
 STATUS.write_text(json.dumps(receipt()));context.clear_cookies();page.goto(base+'/settings/backup');page.wait_for_url('**/login')
 page.close();return {'width':width,'verified_evidence':True,'stale_and_offline_not_green':True,'anonymous_closed':True,'local_receipt_only':True,'automatic_schedule_visible':True,'running_and_failure_separated':True}
f.exercise=exercise
if __name__=='__main__':
 with tempfile.TemporaryDirectory(prefix='kura-backup-status-') as work:
  STATUS=Path(work)/'status.json';os.environ['KURA_BACKUP_STATUS_FILE']=str(STATUS);f.main()
