# agent-doc-guard

지침 문서의 경위 기록을 막고 일반 규칙만 남기는 플러그인

[![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-D97757)](#설치)
[![Codex plugin](https://img.shields.io/badge/Codex-plugin-412991)](#설치)
[![Version](https://img.shields.io/github/v/release/dakcoe/agent-doc-guard)](https://github.com/dakcoe/agent-doc-guard/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[English](README.md)

<table>
<tr><th>에이전트가 AGENTS.md 에 적은 것</th><th>agent-doc-guard 를 쓰면</th></tr>
<tr>
<td><pre>- 10월 5일 DB 컨테이너가 꺼져 있어서
  테스트가 실패했다. 사용자가 직접
  켜야 했다. 맥을 재부팅한 직후 테스트를
  돌릴 때는 DB 컨테이너부터 켠다.</pre></td>
<td><pre>- 테스트 전에 DB 컨테이너를 켠다.</pre></td>
</tr>
</table>

## 만든 이유

에이전트는 작업할 때마다 `AGENTS.md`, `CLAUDE.md`, 스킬을 다시 읽습니다. 그런데 피드백을 받을 때마다 규칙 대신 그때 있었던 일을 적는 경우가 많습니다. 문서는 길어지고, 그 토큰은 이후 모든 작업에서 계속 쓰이며, 에이전트는 지난 일 하나에 맞춰 판단하게 됩니다.

Google 연구진의 [RRSI (2026)](https://arxiv.org/abs/2609.24972) 논문이 이 현상을 측정했습니다. 에이전트 지침을 피드백으로 반복해서 고치자 개선에 쓴 과제의 점수만 오르고 새 과제에서는 향상이 사라졌습니다. 수정마다 특정 과제에만 맞는 내용을 걸러 내자 결과가 달라졌습니다.

| | 거르지 않고 고침 | 걸러 내며 고침 |
|---|---|---|
| 처음 보는 과제에서 향상 | +0.6점 | **+3.9점** |
| 과제당 토큰 | 380만 | **242만 (−36%)** |

<sub>RRSI 표 2, 업무 대행 과제 기준. 처음 보는 과제: JobBench, GDPval, APEX-Agents.</sub>

agent-doc-guard 는 이 거르기를 에이전트가 지침 문서를 고치려는 순간에 적용합니다.

## 설치

**Claude Code**

    /plugin marketplace add dakcoe/agent-doc-guard
    /plugin install agent-doc-guard@agent-doc-guard

<details>
<summary><b>Codex</b></summary>

    codex plugin marketplace add dakcoe/agent-doc-guard
    codex plugin add agent-doc-guard@agent-doc-guard

Codex 는 사용자가 신뢰한 플러그인 훅만 실행합니다. Codex 를 열고 훅 목록(`/hooks`)에서 agent-doc-guard 훅을 신뢰로 바꿉니다.

</details>

<details>
<summary><b>업데이트</b></summary>

개인 마켓플레이스는 자동 업데이트가 기본으로 꺼져 있습니다. Claude Code 에서 `/plugin` → Marketplaces 에서 자동 업데이트를 켜거나, `/plugin marketplace update agent-doc-guard` 를 실행합니다.

</details>

`python3` 가 필요합니다. 판정은 지금 쓰는 에이전트로 돌아갑니다. Claude Code 에서는 `claude -p`, Codex 에서는 `codex exec` 입니다.

## 검사 대상

- `AGENTS.md`, `CLAUDE.md`, `SKILL.md`, `.claude/` 안의 파일, AGENTS.md 가 가리키는 문서를 고칠 때
- 날짜, 지난 일, 한 번 있었던 상황에서 나온 예외 조건, 반복된 규칙이 새로 들어가면 이유와 함께 에이전트에게 돌려보냅니다
- 셸 명령도 검사합니다. 명령 전후로 이 문서들을 비교해서, 검사를 통과하지 못한 변경은 원래대로 되돌립니다
- 줄을 지우는 것은 언제나 통과합니다. 한국어와 영어를 지원합니다
- 사용자가 시킨 내용은 통과합니다. 세션의 최근 사용자 메시지를 함께 읽어서, 사용자가 직접 쓴 줄은 그대로 통과하고 남기라고 한 내용은 그 말을 보고 판정합니다. "기억해 둬"만으로는 통과하지 않고, 에이전트가 일반 규칙으로 바꿔 써야 합니다

## 이미 쌓인 문서 정리

훅은 새로 쌓이는 것을 막습니다. 이미 쌓인 문서는 함께 들어 있는 스킬로 정리합니다. Claude Code 에서 `/agent-doc-guard:doc-cleanup` 를 실행하거나, Codex 에 `doc-cleanup` 스킬을 쓰라고 요청합니다. 지침 문서 목록을 만들고, 백업한 뒤, 한 파일씩 다시 쓰며 첫 파일은 먼저 보여 주고 확인을 받습니다.

## 설정

- `AGENT_DOC_GUARD_JUDGE=off`: 모델 판정 없이 패턴 검사만
- `AGENT_DOC_GUARD_CLAUDE_MODEL`: Claude Code 판정 모델 (기본 `haiku`)
- `AGENT_DOC_GUARD_CODEX_MODEL`: Codex 판정 모델 (기본 `gpt-6-luna`)
- `AGENT_DOC_GUARD_STRICT=1`: 판정 모델을 부르지 못하면 수정을 막습니다 (기본은 경고하고 통과)

## 라이선스

MIT
