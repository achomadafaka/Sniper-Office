import json,time
import sniper_office.codex_child as C
class Slow:
    def __init__(s,items,hang):s.items=items;s.hang=hang
    def __iter__(s):
        yield from s.items
        time.sleep(s.hang)


def test_relay_stall():
    ev=lambda **k:json.dumps(k)
    # stalls with no tool in flight
    r=C.relay({},Slow([ev(type='thread.started',thread_id='t')],5),idle_s=0.3,tool_s=3)
    assert r[3] is True,r
    # tool in flight: longer limit, no stall within 1s
    r=C.relay({},Slow([ev(type='item.started',item=dict(id='a',type='mcp_tool_call'))],1),idle_s=0.3,tool_s=3)
    assert r[3] is False,r
    # normal end
    r=C.relay({},iter([ev(type='turn.completed',usage={'input_tokens':1})]),idle_s=0.3)
    assert r==(None,{'input_tokens':1,'output_tokens':None,'cache_read_input_tokens':None,'cache_creation_input_tokens':None},False,False),r
