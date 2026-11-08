import json
import os
import tempfile
import unittest

import custom_layouts as cl

GOOD = {
    'name': 'Cockpit',
    'panels': ['waiting', 'chat', 'agents', 'status'],
    'grid': {'columns': [{'main': 'waiting', 'width': 250}, {'main': 'chat'}, {'main': 'status', 'width': 220}]},
    'style': {'radius': 4, 'gap': 6},
    'theme': {'bg': '#10151b', 'surface': '#17202a', 'text': '#e3e9ef', 'muted': '#8fa1b3', 'line': '#26323f', 'accent': '#3f8fcf', 'attention': '#e0a23a'},
}


def changed(**parts):
    data = json.loads(json.dumps(GOOD))
    data.update(parts)
    return data


class Validate(unittest.TestCase):
    def test_a_good_layout_comes_back_clean(self):
        layout = cl.validate(GOOD, 'cockpit')
        self.assertEqual(layout['id'], 'cockpit')
        self.assertEqual(layout['style'], {'radius': 4, 'gap': 6, 'surface': 'flat'})
        self.assertEqual(layout['grid']['columns'][1], {'main': 'chat'})
        self.assertEqual(layout['theme']['bg'], '#10151b')

    def test_bad_layouts_are_refused_with_a_reason(self):
        cases = [
            (changed(script='alert(1)'), 'unknown key'),
            (changed(panels=['chat', 'terminal']), 'panels must be'),
            (changed(panels=['waiting', 'status', 'agents']), 'must include chat'),
            (changed(style={'radius': 99}), 'style.radius'),
            (changed(style={'radius': True}), 'style.radius'),
            (changed(style={'surface': 'url(x)'}), 'style.surface'),
            (changed(theme=dict(GOOD['theme'], bg='red')), 'hex colour'),
            (changed(theme=dict(GOOD['theme'], accent='url(https://example.com/x)')), 'hex colour'),
            (changed(theme=dict(GOOD['theme'], css='body{}')), 'unknown key'),
            (changed(theme={'bg': '#000000'}), 'theme needs'),
            (changed(theme=dict(GOOD['theme'], text='#20262c')), 'contrast'),
            (changed(grid={'areas': ['wait main status']}), 'unknown key'),
            (changed(grid={'columns': [{'main': 'chat'}, {'main': 'chat'}]}), 'placed twice'),
            (changed(grid={'columns': [{'main': 'waiting'}, {'main': 'chat'}]}), 'status is in panels'),
            (changed(grid={'columns': [{'top': 'status', 'main': 'chat'}, {'main': 'waiting'}]}), 'can only be waiting'),
            (changed(grid={'columns': [{'main': 'chat'}, {'main': 'waiting', 'width': 900}, {'main': 'status'}]}), 'width'),
            (changed(panels=['chat'], grid={'columns': [{'main': 'chat'}], 'chatWidth': 100}), 'chatWidth'),
            (changed(id='classic'), 'built-in'),
            (changed(id='Bad Id'), 'id must'),
            (changed(name='x' * 41), 'name must'),
        ]
        for data, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(cl.Bad) as caught:
                    cl.validate(data, 'cockpit')
                self.assertIn(reason, str(caught.exception))

    def test_activity_only_leads(self):
        data = changed(panels=['activity', 'chat'], grid={'columns': [{'main': 'chat'}, {'main': 'activity'}]})
        with self.assertRaises(cl.Bad):
            cl.validate(data, 'x')
        data['grid']['columns'].reverse()
        self.assertEqual(cl.validate(data, 'x')['grid']['columns'][0], {'main': 'activity'})


class Scan(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        cl._last_good.clear()

    def write(self, name, data):
        with open(os.path.join(self.folder, name), 'w') as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))

    def test_missing_folder_is_empty(self):
        result = cl.scan(os.path.join(self.folder, 'none'))
        self.assertEqual((result['layouts'], result['errors']), ([], []))

    def test_a_bad_file_keeps_its_last_good_layout(self):
        self.write('Cockpit.json', GOOD)
        self.assertEqual(cl.scan(self.folder)['layouts'][0]['id'], 'cockpit')
        self.write('Cockpit.json', '{not json')
        result = cl.scan(self.folder)
        self.assertEqual(result['layouts'][0]['name'], 'Cockpit')
        self.assertIn('not valid JSON', result['layouts'][0]['stale'])
        self.assertEqual(result['errors'][0]['file'], 'Cockpit.json')

    def test_large_files_links_and_duplicate_ids_are_refused(self):
        self.write('big.json', ' ' * (cl.MAX_BYTES + 1))
        self.write('a.json', changed(id='same'))
        self.write('b.json', changed(id='same'))
        os.symlink(os.path.join(self.folder, 'a.json'), os.path.join(self.folder, 'link.json'))
        result = cl.scan(self.folder)
        self.assertEqual([layout['id'] for layout in result['layouts']], ['same'])
        reasons = {error['file']: error['reason'] for error in result['errors']}
        self.assertIn('16 KB', reasons['big.json'])
        self.assertIn('already uses', reasons['b.json'])
        self.assertIn('plain files', reasons['link.json'])

    def test_folder_label_hides_the_home_path(self):
        self.assertEqual(cl.folder_label(cl.paths.HOME / '.config/x'), '~/.config/x')


if __name__ == '__main__':
    unittest.main()
