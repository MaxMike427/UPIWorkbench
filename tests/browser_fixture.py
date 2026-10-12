"""浏览器验收专用本地服务：真实 API/SQLite，第三方操作全为内存测试替身。"""
import os
import io
import json
import zipfile

import httpx
import uvicorn

from app.services import batch_service
from app.services.qualification_service import CheckOutcome
from app.services.sms_service import SMSService
from app.services.upi_service import UPIService
from app.services.account_export_service import save_account_exports


class Qual:
    def __init__(self,db,**kwargs):pass
    async def check_single(self,at):return CheckOutcome(True,'eligible',{'token_ok':True,'eligible':True})


class UPI(UPIService):
    async def create_with_retries(self,cid,amount,note='',callback_url='',at_token=None):
        async with self.db.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link) VALUES(?,?,'success','https://pay.example/test')",(cid,amount)) as cur:
            task_id=cur.lastrowid
        await self.db.commit()
        return {'id':task_id,'status':'success'}


class SMS(SMSService):
    async def bind_with_retries(self,cid):
        async with self.db.execute("INSERT INTO sms_tasks(credential_id,status,new_access_token) VALUES(?,'bound','eyJbrowser-test')",(cid,)) as cur:
            task_id=cur.lastrowid
        await self.db.commit()
        archives={}
        for fmt in ('cpa','sub2'):
            buf=io.BytesIO()
            with zipfile.ZipFile(buf,'w') as z:z.writestr(fmt+'.json',json.dumps({'access_token':'eyJbrowser-test','format':fmt}))
            archives[fmt]=buf.getvalue()
        await save_account_exports(self.db,cid,task_id,archives)
        return {'id':task_id,'status':'bound'}


batch_service.QualificationService=Qual
batch_service.UPIService=UPI
batch_service.SMSService=SMS
from app.main import app

if __name__=='__main__':
    uvicorn.run(app,host='127.0.0.1',port=int(os.environ.get('UPI_TEST_PORT','8891')))
