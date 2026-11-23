from pathlib import Path

from fastapi.testclient import TestClient

from server.app import create_app

ROOT=Path(__file__).resolve().parents[1]


def test_single_port_flag_is_reported(tmp_path,monkeypatch):
    monkeypatch.delenv('AGENTCHAT_SINGLE_PORT',raising=False)
    assert TestClient(create_app(tmp_path/'a')).get('/api/status').json()['single_port'] is False
    monkeypatch.setenv('AGENTCHAT_SINGLE_PORT','1')
    assert TestClient(create_app(tmp_path/'b')).get('/api/status').json()['single_port'] is True


def test_setup_code_is_logged_only_when_enabled_and_uninitialized(tmp_path,monkeypatch,capsys):
    monkeypatch.delenv('AGENTCHAT_LOG_SETUP_CODE',raising=False)
    create_app(tmp_path)
    code=(tmp_path/'bootstrap-code').read_text(encoding='utf-8').strip()
    assert code not in capsys.readouterr().out

    monkeypatch.setenv('AGENTCHAT_LOG_SETUP_CODE','1')
    app=create_app(tmp_path)
    assert code in capsys.readouterr().out
    TestClient(app).post('/api/bootstrap',json={'username':'owner','password':'nas-deploy-test-password','setup_code':code}).raise_for_status()
    create_app(tmp_path)
    assert code not in capsys.readouterr().out


def test_nas_files_are_self_contained():
    compose=(ROOT/'compose.nas.yaml').read_text(encoding='utf-8')
    assert 'build:' not in compose and './deploy' not in compose
    assert 'image: ghcr.io/jia-yp/agentchat:latest' in compose
    for setting in ('AGENTCHAT_SINGLE_PORT: "1"','AGENTCHAT_LOG_SETUP_CODE: "1"','cap_add: [CHOWN, SETUID, SETGID]'):
        assert setting in compose
    entrypoint=(ROOT/'deploy/docker-entrypoint.sh').read_bytes()
    assert entrypoint.startswith(b'#!/bin/sh\n') and b'\r' not in entrypoint
