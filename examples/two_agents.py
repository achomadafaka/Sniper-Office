"""Two fake agents, one turn each, a tool receipt, then a killed turn that reconciles. No model, no network.

    python3 examples/two_agents.py
"""
import json
from pathlib import Path
import tempfile

from sniper_office import broker as B, config, store, supervisor as S

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    """Stands in for a corrected wall clock; the supervisor refuses an unknown one."""
    now = 1000.
    def __call__(self): return self.now


def main():
    config.load(ROOT / 'config' / 'office.example.json')          # roles, their tools, extra tables
    clock = Clock()
    conn = store.open_store(Path(tempfile.mkdtemp()) / 'office.db')
    for agent_id, role in (('ada', 'Researcher'), ('bo', 'Reviewer')):
        conn.execute('INSERT INTO agents VALUES(?,?)', (agent_id, json.dumps(dict(
            agent_id=agent_id, role=role, state='active', generation=1, policy_hash='policy-1', prompt_hash='prompt-1'))))
    conn.commit()

    lease = S.Lease(conn, clock=clock, ttl_s=100)
    gen = lease.acquire('supervisor-a')                           # one supervisor at a time; gen fences every write

    def read_inbox(args, context):                                # a host-owned tool: the agent only names it
        return B.ToolResult(result={'items': 3}, units={'items': 'count'}, network_id=None, source_refs=[], coverage='observed')

    def step(context, previous, remaining_s):                     # your model call goes here
        receipt = broker.execute(B.ToolRequest(context.actor_id, 'intake.read', {}, context.decision_id, context.cut), context=context)
        return S.Step(done=True, value={'outcome': receipt.outcome, 'receipt': receipt.receipt_hash[:12], 'result': receipt.result})

    def freeze_cut():                                             # what the turn is allowed to know, fixed before it starts
        return {'basis': 'corrected_utc', 'receipt_cut': clock(), 'knowledge_cut': clock(), 'slot_cut': None,
                'clock_sample': {'corrected_utc': clock(), 'offset_s': 0}}

    sup = S.Supervisor(conn, lease=lease, clock=clock, step=step, freeze_cut=freeze_cut)
    broker = B.Broker(conn, None, {'intake.read': B.Tool(read_inbox, {}, 'inbox-v1')}, generation_check=sup.valid_context)

    for agent_id in ('ada', 'bo'):                                # ada's role may read the inbox; bo's may not
        result = sup.run_turn(agent_id, gen=gen, deadline_s=5, max_steps=1, turn_id='turn-' + agent_id)
        print(agent_id, result.state, result.value)
    print('receipt chain intact:', store.verify_chain(conn, 'receipts'))

    def dies(*_):
        raise SystemExit('process killed mid-turn')
    try:
        S.Supervisor(conn, lease=lease, clock=clock, step=dies, freeze_cut=freeze_cut).run_turn(
            'ada', gen=gen, deadline_s=2, max_steps=1, turn_id='turn-killed')
    except SystemExit:
        pass
    print('after kill:', S._get(conn, 'turn_runs', 'turn-killed')['state'])

    clock.now += 3                                                # past the turn's deadline
    lease.release(gen)
    new_gen = lease.acquire('supervisor-b')                       # the restarted supervisor
    sup.reconcile(gen=new_gen)
    record = S._get(conn, 'turn_runs', 'turn-killed')
    print('after reconcile:', record['state'], record['reason'])
    try:
        sup.run_turn('ada', gen=gen, deadline_s=5, max_steps=1)   # the old supervisor's generation
    except S.StaleGeneration:
        print('old generation refused: StaleGeneration')
    conn.close()


if __name__ == '__main__':
    main()
