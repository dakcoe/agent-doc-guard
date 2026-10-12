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

Google 연구진의 [RRSI (2026)](https://arxiv.org/abs/2609.24972) 논문이 같은 현상을 측정했습니다. 에이전트 하네스(프롬프트, 제어 흐름, 도구, 메모리)를 정해진 과제 묶음에 맞춰 반복해서 고치자, 그 과제의 점수는 올랐지만 새 과제에서의 향상은 거의 사라졌습니다. RRSI는 이런 수정 과정에 여러 제약을 걸고, 그중 하나로 특정 과제에만 맞춘 변경을 걸러 냅니다. 아래 수치는 논문의 결과입니다.

| | 제약 없이 고침 | RRSI (제약 전체) |
|---|---|---|
| 처음 보는 과제에서 향상 | +0.6점 | **+3.9점** |
| 시도당 토큰 | 380만 | **242만 (−36%)** |

<sub>RRSI 표 2, 업무 대행 과제 기준. 처음 보는 과제: JobBench, GDPval, APEX-Agents. 이 향상은 RRSI 제약 전체를 함께 쓴 결과이며, 논문은 거르기 하나만의 효과를 따로 측정하지 않았습니다.</sub>

agent-doc-guard 는 그 거르기의 기준인 "이 변경이 처음 보는 과제에서도 도움이 되는가"를, 에이전트가 지침 문서에 넣는 줄마다 묻습니다. 논문의 방법을 지침 문서 편집에 옮겨 적용한 플러그인이며, 이 플러그인 자체로 벤치마크 점수를 측정한 적은 없습니다.

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

`python3` 가 필요합니다. 검토는 지금 쓰는 에이전트로 돌아갑니다. Claude Code 에서는 `claude -p`, Codex 에서는 `codex exec` 입니다.

## 검사 대상

- `AGENTS.md`, `CLAUDE.md`, `SKILL.md`, `.claude/` 안의 파일, AGENTS.md 가 가리키는 문서를 고칠 때
- 아무것도 막거나 되돌리지 않습니다. 수정으로 이 문서에 줄이 들어가면 수정 뒤에 에이전트에게 안내를 붙입니다
  - 세션에서 그 파일을 처음 고쳤을 때: 규칙과 방금 넣은 줄
  - 매 수정: 상관없는 다음 작업에서 도움이 되지 않는 줄(날짜, 지난 일, 한 번 있었던 상황에서 나온 예외 조건, 반복된 규칙)과 고칠 방법. 작은 모델(Claude Code 에서는 Haiku)이 찾습니다. 남겨야 할 날짜나 경위는 검사하지 않는 같은 폴더의 `CHANGELOG.md` 로 보냅니다
- 사용자가 시킨 내용은 지적하지 않습니다. 세션의 최근 사용자 메시지를 함께 읽어서, 사용자가 직접 쓴 줄은 그대로 두고 남기라고 한 내용은 그 말을 보고 판단합니다. "기억해 둬"만으로는 넘어가지 않고, 에이전트가 일반 규칙으로 바꿔 써야 합니다
- 셸 명령이 이 문서에 내용을 쓰면 실행 뒤에 쓴 내용을 확인하라는 안내를 붙입니다. 파일 복사·클론·이동·삭제는 건드리지 않습니다
- 줄을 지우는 수정에는 안내를 붙이지 않습니다. 한국어와 영어를 지원합니다

## 이미 쌓인 문서 정리

훅은 새로 쌓이는 것을 잡아냅니다. 이미 쌓인 문서는 함께 들어 있는 스킬로 정리합니다. Claude Code 에서 `/agent-doc-guard:doc-cleanup` 를 실행하거나, Codex 에 `doc-cleanup` 스킬을 쓰라고 요청합니다. 지침 문서 목록을 만들고, 백업한 뒤, 한 파일씩 다시 쓰며 첫 파일은 먼저 보여 주고 확인을 받습니다.

## 설정

- `AGENT_DOC_GUARD_JUDGE=off`: 모델을 부르지 않고 정해진 단어 패턴으로만 검사
- `AGENT_DOC_GUARD_CLAUDE_MODEL`: Claude Code 검토 모델 (기본 `haiku`)
- `AGENT_DOC_GUARD_CODEX_MODEL`: Codex 검토 모델 (기본 `gpt-6-luna`)

## 라이선스

MIT
