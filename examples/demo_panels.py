"""Three project panels, one of each shape. Start the page with: --plugins examples.demo_panels"""
import math


def register(host):
    host.add_panel('open_items', 'Open items', lambda now: dict(shape='table', columns=['Item', 'Owner', 'Age (h)', 'Done'], as_of=now, note='Made-up rows.',
                                                               rows=[['Filing summary', 'researcher', 3.5, False], ['Citation check', 'reviewer', 1.0, False], ['<b>markup is text</b>', None, 0.2, True]]))
    host.add_panel('numbers', 'Numbers', lambda now: dict(shape='facts', as_of=now, items=[dict(label='Items closed today', value=7, unit='items', as_of=now - 600),
                                                                                           dict(label='Oldest open item', value=3.5, unit='h'), dict(label='Backlog source', value=None)]))
    host.add_panel('throughput', 'Throughput', lambda now: dict(shape='series', unit='items per hour', as_of=now,
                                                                points=[[now - (48 - i) * 3600, 4 + 3 * math.sin(i / 5.)] for i in range(48)]))
    host.add_panel('broken', 'Broken panel', lambda now: 1 / 0)
