import httpx

from app.diagnosis.gateway import ReasoningGatewayClient


def test_gateway_fails_over_between_registered_models_and_audits_selection(monkeypatch):
    attempts=[]

    class Response:
        def __init__(self,model): self.model=model
        def raise_for_status(self):
            if self.model=="model-a": raise httpx.HTTPStatusError("failed",request=None,response=None)
        def json(self): return {"proposal":{"schema_version":"ai-proposal-v1"}}

    class Client:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self,url,json,headers):
            attempts.append(json["model"])
            return Response(json["model"])

    monkeypatch.setattr("app.diagnosis.gateway.httpx.Client",Client)
    monkeypatch.setattr("app.diagnosis.gateway.settings.reasoning_gateway_models","model-a,model-b")
    monkeypatch.setattr("app.diagnosis.gateway.settings.reasoning_gateway_failover_enabled",True)
    client=ReasoningGatewayClient(url="https://gateway.invalid",model="model-a")
    result=client.enhance({"case":{"summary":"noise"}}, {})
    assert attempts==["model-a","model-b"]
    assert result["_routing"]=={"selected_model":"model-b","attempt":2,"failover":True}
    assert client.model=="model-b"
