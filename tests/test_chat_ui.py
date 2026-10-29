"""Offline UI contracts, executed against the app's actual JavaScript with Node."""
import json
from pathlib import Path

import pytest

from test_public import _node


def js():
    source = Path('sleeper_gm/web/app.js').read_text()
    helpers = source[source.index('  function esc('):source.index('  var fTime')]
    chat = source[source.index('  /* ---------- ask ---------- */'):source.index('  /* ---------- shell ---------- */')]
    return helpers + chat


@pytest.mark.parametrize('text,expected', [
    ('Use **25 points**.\n\nThen wait.', '<p>Use <strong>25 points</strong>.</p><p>Then wait.</p>'),
    ('- First\n- **Second**', '<ul><li>First</li><li><strong>Second</strong></li></ul>'),
    ('<img src=x onerror=alert(1)>', '<p>&lt;img src=x onerror=alert(1)&gt;</p>'),
    ('**<script>bad()</script>** & "safe"', '<p><strong>&lt;script&gt;bad()&lt;/script&gt;</strong> &amp; &quot;safe&quot;</p>'),
    ('[click](javascript:bad())', '<p>[click](javascript:bad())</p>'),
])
def test_chat_renders_only_escaped_paragraphs_bold_and_lists(text, expected):
    assert _node(js() + '\nconsole.log(JSON.stringify(chatText(' + json.dumps(text) + ')));') == expected


@pytest.mark.parametrize('first,second,win,add', [
    ('Avery Example', 'Blake Sample', .42, 'Casey Example'),
    ('Devon Sample', 'Ellis Example', .71, 'Finley Sample'),
])
def test_starter_questions_come_from_slot_order_odds_and_claims(first, second, win, add):
    snap = {'p_win': win, 'opp': {'name': 'Opponent'}, 'days_left': ['2026-10-29'],
            'me': {'slots': [{'state': 'open', 'plan': {'looks': [{'id': 'a', 'name': first}, {'id': 'b', 'name': second}]}}]},
            'claims': {'bot': [{'add': add}]}}
    result = _node(js() + '\nconsole.log(JSON.stringify(starterQuestions(' + json.dumps(snap) + ')));')
    assert len(result) == 3
    assert result[0] == f'Why {first.split()[-1]} before {second.split()[-1]}?'
    assert any(str(round(win * 100)) + '%' in q for q in result)
    assert any(add in q for q in result)


@pytest.mark.parametrize('active', [True, False])
def test_saved_action_undo_only_targets_current_directive(active):
    action = {'id': 'version-one', 'label': 'never drop <Example>'}
    script = 'var S = {chat: {directives: ' + json.dumps([action] if active else []) + '}};\n'
    html = _node(script + js() + '\nconsole.log(JSON.stringify(chatActions(' + json.dumps([action]) + ', [])));')
    assert 'Saved: never drop &lt;Example&gt;' in html
    assert ('data-undo="version-one"' in html) is active
    assert '<Example>' not in html


@pytest.mark.parametrize('status,payload,expected', [
    (200, {'history': []}, None),
    (503, {'error': 'Chat is off. Set chat.command in your config.'}, 'Chat is off. Set chat.command in your config.'),
    (409, {'error': 'The GM is already answering. Wait for that reply.'}, 'The GM is already answering. Wait for that reply.'),
    (504, {'error': 'The GM took too long. Try again.'}, 'The GM took too long. Try again.'),
])
def test_chat_requests_use_json_and_surface_server_errors(status, payload, expected):
    script = '''
var seen;
const fetch = async (url, options) => {
  seen = {url, options};
  return new Response(PAYLOAD, {status: STATUS, headers: {'content-type': 'application/json'}});
};
'''.replace('PAYLOAD', json.dumps(json.dumps(payload))).replace('STATUS', str(status))
    result = _node(script + js() + '''
chatRequest('chat', '123', {message: 'why?'}).then(
  data => console.log(JSON.stringify({seen, data})),
  error => console.log(JSON.stringify({seen, error: error.message})));
''')
    assert result['seen']['url'] == 'api/chat?league=123'
    assert result['seen']['options']['method'] == 'POST'
    assert result['seen']['options']['cache'] == 'no-store'
    assert result['seen']['options']['headers']['content-type'] == 'application/json'
    assert result.get('error') == expected


@pytest.mark.parametrize('key,shift,composing,sends', [('Enter', False, False, 1), ('Enter', True, False, 0), ('Enter', False, True, 0), ('a', False, False, 0)])
def test_composer_enter_sends_but_shift_enter_and_ime_do_not(key, shift, composing, sends):
    result = _node(js() + '''
let sent = 0, prevented = 0;
sendChat = () => sent++;
chatKeydown({key: KEY, shiftKey: SHIFT, isComposing: COMPOSING, preventDefault: () => prevented++});
console.log(JSON.stringify({sent, prevented}));
'''.replace('KEY', json.dumps(key)).replace('SHIFT', json.dumps(shift)).replace('COMPOSING', json.dumps(composing)))
    assert result == {'sent': sends, 'prevented': sends}


@pytest.mark.parametrize('height,expected', [(24, 24), (70, 70), (200, 96)])
def test_composer_growth_is_capped_at_four_lines(height, expected):
    result = _node(js() + '''
const input = {scrollHeight: HEIGHT, style: {}};
const $ = () => input;
resizeComposer();
console.log(JSON.stringify(input.style));
'''.replace('HEIGHT', str(height)))
    assert result['height'] == str(expected) + 'px'
    assert result['overflowY'] == ('auto' if height > 96 else 'hidden')


@pytest.mark.parametrize('path,method', [('/api/chat?league=123', 'GET'), ('/api/now', 'GET'), ('/api/chat', 'POST'), ('/api/directives', 'POST')])
def test_service_worker_never_intercepts_api_requests(path, method):
    source = Path('sleeper_gm/web/sw.js').read_text()
    result = _node('''
const handlers = {};
const self = {location: {origin: 'https://gm.test'}, addEventListener: (name, cb) => handlers[name] = cb};
const caches = {open: () => {throw new Error('API touched cache');}};
''' + source + '''
let intercepted = false;
handlers.fetch({request: new Request('https://gm.test' + PATH, {method: METHOD}), respondWith: () => intercepted = true});
console.log(JSON.stringify(intercepted));
'''.replace('PATH', json.dumps(path)).replace('METHOD', json.dumps(method)))
    assert result is False


@pytest.mark.parametrize('switched,failed', [(False, False), (True, False), (False, True)])
def test_send_keeps_draft_on_failure_and_never_mixes_leagues(switched, failed):
    result = _node(js() + '''
var S = {league: '123', now: {live: false}};
const original = {league: '123', draft: 'why?', enabled: true, history: [], directives: [], busy: false};
S.chat = original;
chatState = () => S.chat;
renderAsk = () => {};
const renderHeader = () => {}, renderBoard = () => {};
let finish;
const fetch = () => new Promise(resolve => finish = resolve);
const pending = sendChat();
const thinking = original.busy && original.pending === 'why?';
if (SWITCHED) { S.chat = {league: '456', history: [], directives: [], draft: 'new team'}; S.now = {live: false}; }
finish(new Response(JSON.stringify(FAILED ? {error: 'Timed out. Try again.'} : {
  history: [{role: 'user', text: 'why?'}, {role: 'assistant', text: '25 beats 24.'}],
  directives: [{id: 'keep-a', label: 'never drop A'}], live: true
}), {status: FAILED ? 504 : 200}));
pending.then(() => console.log(JSON.stringify({thinking, original, current: S.chat, now: S.now})));
'''.replace('SWITCHED', json.dumps(switched)).replace('FAILED', json.dumps(failed)))
    assert result['thinking'] is True
    assert result['original']['busy'] is False
    assert result['original']['draft'] == ('why?' if failed else '')
    assert bool(result['original']['error']) is failed
    if switched:
        assert result['current']['history'] == []
        assert result['current']['draft'] == 'new team'
        assert result['now']['live'] is False
    elif not failed:
        assert result['current']['history'][-1]['text'] == '25 beats 24.'
        assert result['now']['directives'][0]['id'] == 'keep-a'
        assert result['now']['live'] is True


@pytest.mark.parametrize('endpoint,payload', [('chat/clear', {}), ('directives', {'id': 'keep-a'})])
def test_clear_and_undo_use_server_result_and_do_not_erase_draft(endpoint, payload):
    result = _node(js() + '''
var S = {league: '123', now: {directives: [{id: 'keep-a'}]}, chat: {
  league: '123', history: [{text: 'old'}], directives: [{id: 'keep-a'}], draft: 'keep typing', busy: false
}};
chatState = () => S.chat;
renderAsk = () => {};
const renderHeader = () => {}, renderBoard = () => {};
let request;
const fetch = async (url, options) => {
  request = {url, body: JSON.parse(options.body)};
  return new Response(JSON.stringify({history: [], directives: []}));
};
changeChat(ENDPOINT, PAYLOAD).then(() => console.log(JSON.stringify({request, chat: S.chat, now: S.now})));
'''.replace('ENDPOINT', json.dumps(endpoint)).replace('PAYLOAD', json.dumps(payload)))
    assert result['request'] == {'url': 'api/' + endpoint + '?league=123', 'body': payload}
    assert result['chat']['history'] == []
    assert result['chat']['directives'] == result['now']['directives'] == []
    assert result['chat']['draft'] == 'keep typing'


@pytest.mark.parametrize('changed', [False, True])
def test_refresh_cannot_replace_newer_chat_result(changed):
    result = _node(js() + '''
var S = {league: '123', now: {}, chat: {
  league: '123', enabled: true, loaded: true, history: [], directives: [], revision: 0
}};
chatState = () => S.chat;
renderAsk = () => {};
const renderHeader = () => {}, renderBoard = () => {};
let finish;
const fetch = () => new Promise(resolve => finish = resolve);
const request = loadChat();
if (CHANGED) { S.chat.revision++; S.chat.history = [{text: 'new answer'}]; }
finish(new Response(JSON.stringify({history: [{text: 'old answer'}], directives: []})));
request.then(() => console.log(JSON.stringify(S.chat.history)));
'''.replace('CHANGED', json.dumps(changed)))
    assert result == [{'text': 'new answer' if changed else 'old answer'}]


@pytest.mark.parametrize('threshold,decision,expected', [(None, 'lock', 'help you win'), (25, 'lock', 'Your lock line is 25.0'), (35, 'pass', 'Your lock line is 35.0')])
def test_board_explains_owner_lock_override_without_claiming_a_model_edge(threshold, decision, expected):
    source = Path('sleeper_gm/web/app.js').read_text()
    helpers = source[source.index('  function esc('):source.index('  var fTime')]
    render = source[source.index('  function slotRow('):source.index('  function benchRow(')]
    row = {'slot': 'G', 'state': 'pending', 'score': 25, 'open_value': 32, 'decision': decision,
           'player': {'id': '1', 'name': 'Example Player'}}
    if threshold is not None:
        row['owner_lock_at'] = threshold
    script = helpers + render + '''
const nextGame = () => null, tag = () => '', barHtml = () => '', crowdLine = () => '', planHtml = () => '', playerFacts = () => '';
const rowWrap = (key, cls, inner, detail) => inner;
console.log(JSON.stringify(slotRow(ROW, 0, 40, {pwin: .4})));
'''.replace('ROW', json.dumps(row))
    rendered = _node(script)
    assert expected in rendered
    if threshold is not None:
        assert 'help you win' not in rendered


@pytest.mark.parametrize('status,uses_cache', [(403, False), (500, True)])
def test_board_never_uses_cached_data_after_access_denied(status, uses_cache):
    source = Path('sleeper_gm/web/app.js').read_text()
    helpers = source[source.index('  function esc('):source.index('  var fTime')]
    data = source[source.index('  function cacheGet('):source.index('  function load(')]
    script = '''
var S = {}, CACHE_MAX_AGE = 604800000;
const removed = [];
const localStorage = {getItem: () => JSON.stringify({t:Date.now(),data:{private:'saved'}}), removeItem: key => removed.push(key)};
const fetch = async () => new Response('{}', {status:STATUS});
'''.replace('STATUS', str(status)) + helpers + data + '''
getJSON('now').then(result => console.log(JSON.stringify({result,removed})), error => console.log(JSON.stringify({error:error.status,removed})));
'''
    result = _node(script)
    assert ('result' in result) is uses_cache
    assert bool(result['removed']) is (not uses_cache)
    if not uses_cache:
        assert result['error'] == 403


@pytest.mark.parametrize('enabled', [True, False])
def test_clear_preserves_off_explanation(enabled):
    result = _node(js() + '''
var S = {league: '123', now: {}, chat: {league:'123',history:[],directives:[],draft:'',busy:false}};
chatState = () => S.chat;
renderAsk = () => {};
const renderHeader = () => {}, renderBoard = () => {};
const fetch = async () => new Response(JSON.stringify({enabled:ENABLED,history:[],directives:[],error: ENABLED ? null : 'Chat is off. Set chat.command in your config.'}));
changeChat('chat/clear', {}).then(() => console.log(JSON.stringify(S.chat)));
'''.replace('ENABLED', json.dumps(enabled)))
    assert result['enabled'] is enabled
    assert result['error'] == ('' if enabled else 'Chat is off. Set chat.command in your config.')


@pytest.mark.parametrize('enabled,busy', [(False, False), (True, False), (True, True)])
def test_render_ask_off_thinking_directives_and_refusals(enabled, busy):
    result = _node(js() + '''
var S = {now: {}, chat: {
 enabled: ENABLED, busy: BUSY, history: [{role:'assistant',text:'A real answer',actions:[],refused:[{reason:'<Not allowed>'}]}],
 directives: [{id:'keep-a',label:'never drop <A>'}], draft:'why?', pending: BUSY ? 'why?' : null,
 error: ENABLED ? '' : 'Chat is off. Set chat.command in your config.'
}};
const elements = {};
const $ = id => elements[id] || (elements[id] = {style:{},scrollHeight:40,scrollTop:0,clientHeight:200});
chatState = () => S.chat;
renderAsk();
console.log(JSON.stringify(elements));
'''.replace('ENABLED', json.dumps(enabled)).replace('BUSY', json.dumps(busy)))
    assert result['chat-input']['disabled'] is (not enabled)
    assert result['chat-send']['disabled'] is (busy or not enabled)
    assert 'never drop &lt;A&gt;' in result['chat-directives']['innerHTML']
    assert 'Not saved: &lt;Not allowed&gt;' in result['chat-messages']['innerHTML']
    assert ('GM is thinking...' in result['chat-messages']['innerHTML']) is busy
    assert result['chat-error']['hidden'] is enabled


@pytest.mark.parametrize('height,top,expected', [(844, 0, 0), (500, 0, 344), (500, 10, 334)])
def test_keyboard_viewport_lifts_tabbar_and_preserves_composer_space(height, top, expected):
    result = _node(js() + '''
const styles = {};
const window = {innerHeight:844,visualViewport:{height:HEIGHT,offsetTop:TOP}};
const document = {documentElement:{style:{setProperty:(key,value) => styles[key] = value}}};
askViewport();
console.log(JSON.stringify(styles));
'''.replace('HEIGHT', str(height)).replace('TOP', str(top)))
    assert result == {'--ask-height': f'{height}px', '--ask-top': f'{top}px', '--keyboard-lift': f'{expected}px'}


@pytest.mark.parametrize('switch_league', [False, True])
def test_league_switch_during_board_load_cannot_mix_chat_context(switch_league):
    source = Path('sleeper_gm/web/app.js').read_text()
    helpers = source[source.index('  function esc('):source.index('  var fTime')]
    loader = source[source.index('  function load('):source.index('  /* ---------- header')]
    result = _node(helpers + loader + '''
var S = {league:'123',loading:false,loadRevision:0}, qs = new URLSearchParams('league=123');
const localStorage = {getItem: () => null};
const currentTab = () => 'ask', render = () => {};
const contexts = [];
const loadChat = () => contexts.push({league:S.league,now:S.now.league.id});
let resolveOld;
const getJSON = (name, params) => {
 if (name === 'leagues') return Promise.resolve({data:[{id:'123'}, {id:'456'}]});
 if (name === 'now' && params.league === '123') return new Promise(resolve => resolveOld=resolve);
 if (name === 'now') return Promise.resolve({data:{league:{id:'456',week:2}}});
 return Promise.resolve({data:[]});
};
(async () => {
 load(); await new Promise(setImmediate);
 if (SWITCH) { S.league='456'; qs.set('league','456'); S.loadRevision++; S.loading=false; load(); await new Promise(setImmediate); }
 resolveOld({data:{league:{id:'123',week:2}}});
 await new Promise(setImmediate);
 console.log(JSON.stringify({contexts,league:S.league,now:S.now.league.id}));
})();
'''.replace('SWITCH', json.dumps(switch_league)))
    expected = '456' if switch_league else '123'
    assert result['league'] == result['now'] == expected
    assert result['contexts'] == [{'league': expected, 'now': expected}]


@pytest.mark.parametrize('status', [403, 500])
def test_any_denied_board_read_clears_visible_private_state(status):
    source = Path('sleeper_gm/web/app.js').read_text()
    helpers = source[source.index('  function esc('):source.index('  var fTime')]
    loader = source[source.index('  function load('):source.index('  /* ---------- header')]
    result = _node(helpers + loader + '''
var S = {league:'123',loadRevision:0,runs:[{old:true}],season:[{old:true}]}, qs = new URLSearchParams('league=123');
const localStorage = {getItem: () => null};
const currentTab = () => 'board', render = () => {};
const getJSON = (name) => {
 if (name === 'leagues') return Promise.resolve({data:[{id:'123'}]});
 if (name === 'now') return Promise.resolve({data:{league:{id:'123',week:2}}});
 const e = new Error('denied'); e.status=STATUS; e.kind=STATUS===403?'denied':'http'; return Promise.reject(e);
};
load();
setImmediate(() => console.log(JSON.stringify(S)));
'''.replace('STATUS', str(status)))
    assert bool(result.get('now')) is (status != 403)
    if status == 403:
        assert result['runs'] == result['season'] == []
        assert result['error'] == 'denied'
