from fastapi.responses import StreamingResponse


class ManagedStreamingResponse(StreamingResponse):
    """Close upstream resources even if sending response headers is cancelled."""
    def __init__(self,*args,on_finish,**kwargs):
        super().__init__(*args,**kwargs)
        self.on_finish=on_finish

    async def __call__(self,scope,receive,send):
        try:
            await super().__call__(scope,receive,send)
        finally:
            await self.on_finish()
