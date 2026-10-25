import json
import sys
import tomllib
from pathlib import Path

import pytest

from campus_runner.client_config import CLIENTS, export_configs, launch_config, render_config
from campus_runner.mcp_server import handle


def test_export_all_uses_same_launch_and_does_not_create_credentials(tmp_path):
    home = tmp_path/'状态目录 with spaces'
    output = tmp_path/'client configurations'
    result = export_configs(home, 8899, output)
    assert len(result['files']) == len(CLIENTS)
    expected = launch_config(home, 8899)
    for client in ('generic','claude-code','cursor','workbuddy'):
        value=json.loads((output/(client+'.mcp.json')).read_text(encoding='utf-8'))['mcpServers']['campus-runner']
        for key in expected:
            assert value[key] == expected[key]
        if client in ('claude-code','workbuddy'):
            assert value['type']=='stdio'
    toml=tomllib.loads((output/'codex.toml').read_text(encoding='utf-8'))['mcp_servers']['campus-runner']
    assert toml['command']==expected['command']
    assert toml['args']==expected['args']
    assert toml['env']==expected['env']
    patch=(output/'deepseek-harness.patch.yml').read_text(encoding='utf-8')
    assert patch.startswith('- insert:\n')
    assert '@deepseek-ai/dsh-mcp-client' in patch
    assert 'transport: stdio' in patch
    assert json.dumps(expected['args'],ensure_ascii=False) in patch
    assert not home.exists()
    assert 'password' not in ''.join(Path(p).read_text(encoding='utf-8') for p in result['files'])


@pytest.mark.parametrize('client',CLIENTS)
def test_single_export_and_templates_parse(tmp_path,client):
    result=export_configs(tmp_path/'state',8765,tmp_path/'out',client)
    assert len(result['files'])==1
    template=Path('configs')/Path(result['files'][0]).name
    assert template.exists()
    if template.suffix=='.json':
        assert 'campus-runner' in json.loads(template.read_text(encoding='utf-8'))['mcpServers']
    if template.suffix=='.toml':
        assert 'campus-runner' in tomllib.loads(template.read_text(encoding='utf-8'))['mcp_servers']


def test_bad_client_does_not_write_output(tmp_path):
    with pytest.raises(ValueError):
        export_configs(tmp_path/'state',8765,tmp_path/'out','unknown')
    assert not (tmp_path/'out').exists()
    with pytest.raises(ValueError):
        launch_config(tmp_path,0)


def test_protocol_fallback_and_client_neutral_instructions():
    response=handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
        'protocolVersion':'2026-07-28','clientInfo':{'name':'other-agent','version':'1'},'capabilities':{}}},lambda *args:None)
    assert response['result']['protocolVersion']=='2025-11-25'
    assert 'Agent' in response['result']['instructions']
    assert 'Codex' not in response['result']['instructions']


def test_snapshot_excludes_other_agent_credentials(service,tmp_path):
    root=tmp_path/'project-agents';root.mkdir()
    (root/'train.py').write_text('print(1)')
    for directory in ('.claude','.cursor','.workbuddy','.dsh'):
        folder=root/directory;folder.mkdir();(folder/'settings.json').write_text('private-client-token')
    for name in ('.mcp.json','.claude.json','cordis.patch.yml'):
        (root/name).write_text('private-client-token')
    snapshot=service.prepare_snapshot(str(root))
    assert [f['path'] for f in snapshot['manifest']['files']]==['train.py']
