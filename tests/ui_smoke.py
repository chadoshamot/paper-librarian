"""Optional real-browser UI smoke test, using a temporary library and mocked agent."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
from service import web

SCRIPT = r"""
<script>
window.addEventListener('load', async()=>{
  const result=document.createElement('pre');result.id='ui-test-result';result.hidden=true;document.body.appendChild(result);
  const checks=[];
  function check(value,name){if(!value)throw Error(name);checks.push(name);}
  try {
    switchTab('daily');$('#profile-editor').open=true;await loadProfile();
    check($('#profile-queries').value.includes('graph neural networks'),'profile loaded');
    $('#profile-body').value='# 我的研究\n优先推荐可复现的稀疏图工作';await saveProfile();
    check(PROFILE.requirements.includes('稀疏图'),'form save persists');
    $('#profile-raw-mode').open=true;syncProfileRaw();
    $('#profile-raw').value+='\n排除纯理论工作';$('#profile-raw').dispatchEvent(new Event('input'));
    $('#profile-raw-mode').open=false;await saveProfile();
    check(PROFILE.requirements.includes('排除纯理论工作'),'raw edit survives collapse');
    $('#profile-top').value=-1;await saveProfile();
    check($('#profile-status').textContent.includes('top_k'),'invalid config explained');
    await loadProfile();
    $('#chat-input').value='帮我整理论文';await sendChat();
    check(chatBusy,'chat send locked');
    newChat();check(hist.length===1,'cannot switch while running');
    const started=Date.now();
    while(chatBusy && Date.now()-started<7000)await new Promise(r=>setTimeout(r,100));
    check(!chatBusy && hist.length===2,'agent reply displayed');
    check(document.querySelector('.tool-trace'),'tool trace displayed');
    check(!$('#chat-send').disabled,'send unlocked after result');
    result.textContent='UI_TEST_PASS '+checks.join(' | ');
  }catch(e){result.textContent='UI_TEST_FAIL '+e.message+' | '+checks.join(' | ');}
});
</script>
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    args = parser.parse_args()
    if not Path(args.browser).exists():
        raise SystemExit('Supply an installed Chromium browser with --browser')
    output = ROOT / 'reports' / 'ui-check'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        options = yaml.safe_load((ROOT / 'config.example.yml').read_text(encoding='utf-8'))
        options['daily']['queries'] = ['graph neural networks']
        cfg = SimpleNamespace(root=root, config=options, model=options['model'], cache_dir=root/'cache',
                              taxonomy=yaml.safe_load((ROOT/'taxonomy.yml').read_text(encoding='utf-8')))

        class QAHandler(web.Handler):
            def _html(self, text, status=200):
                return super()._html(text.replace('</body>', SCRIPT+'</body>'), status)

        def fake_agent(*args, **kwargs):
            return {'answer': '已检查论文库，暂无待整理的论文。', 'citations': [],
                    'tool_events': [{'tool': 'audit_library', 'status': 'done', 'label': '完成整理检查'}]}

        server = web.ThreadingHTTPServer(('127.0.0.1', 0), QAHandler)
        with patch.object(web, '_CONFIG', cfg), patch.object(web, '_run_librarian', fake_agent):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                process = subprocess.run([
                    args.browser, '--headless', '--disable-gpu', '--no-sandbox', '--no-first-run',
                    '--disable-background-networking', '--no-default-browser-check',
                    '--user-data-dir='+str(root/'browser'), '--window-size=1400,1000',
                    '--virtual-time-budget=12000', '--dump-dom',
                    '--screenshot='+str(output/'daily.png'),
                    f'http://127.0.0.1:{server.server_port}/'],
                    capture_output=True, timeout=40,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                dom = process.stdout.decode('utf-8', errors='replace')
                (output/'dom.html').write_text(dom, encoding='utf-8')
                (output/'browser.log').write_bytes(process.stderr)
                import re
                match = re.search(r'<pre id="ui-test-result"[^>]*>(.*?)</pre>', dom, re.S)
                result = match.group(1) if match else 'UI_TEST_FAIL missing test result'
                print(result)
                if not result.startswith('UI_TEST_PASS'):
                    raise SystemExit(1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    main()
