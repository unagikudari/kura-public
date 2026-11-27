"""Private read-only training readiness, mounted at the existing dashboard API."""
from fastapi import APIRouter,Depends,Request
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from api.auth import get_current_user
from api.db.models import User
from api.db.session import engine
from api.errors import KuraError
from api.services import training_readiness as service


class PrivateReadinessRoute(APIRoute):
    def get_route_handler(self):
        handler=super().get_route_handler()
        async def private(request:Request):
            try:
                response=await handler(request)
            except (KuraError,HTTPException,RequestValidationError) as exc:
                if isinstance(exc,RequestValidationError):
                    exc=KuraError(422,'training_readiness_request_invalid',fix='Use the frozen queue reference.')
                status=exc.status if isinstance(exc,KuraError) else exc.status_code
                detail=exc.body() if isinstance(exc,KuraError) else exc.detail
                response=JSONResponse(status_code=status,content={'detail':detail},headers=getattr(exc,'headers',None))
            response.headers['Cache-Control']='private, no-store'
            return response
        return private


router=APIRouter(prefix="/api",route_class=PrivateReadinessRoute)


@router.get('/tagger-dashboard/review-queues/{ref}/training-readiness')
async def training_readiness(ref:str,user:User=Depends(get_current_user)):
    return await service.read(ref,engine)
