from __future__ import annotations

from sqlalchemy.orm import Session

from app.knowledge.service import search_knowledge_items


def answer_verified_question(db: Session, query: str, *, limit: int = 2) -> dict:
    """Answer only from reviewer-verified KnowledgeItems.

    This is intentionally extractive and deterministic. It does not synthesize
    protocol facts and therefore remains safe while the AI gateway is disabled.
    """
    matches = [item for item in search_knowledge_items(db, query, limit=max(1, limit * 3))
               if float(item.get('score') or 0) >= 0.03][:limit]
    if not matches:
        return {
            'answered': False,
            'text': '当前已审核知识库中没有找到足够匹配的答案。如果这是现场故障，请描述现象并提供设备信息或上传抓包。',
            'citations': [],
        }
    sections = []
    for item in matches:
        summary = str(item.get('summary') or '').strip()
        sections.append(f'《{item["title"]}》：{summary}')
    titles = '、'.join(f'《{item["title"]}》' for item in matches)
    return {
        'answered': True,
        'text': f'{" ".join(sections)}\n来源：已审核知识库 {titles}。',
        'citations': [
            {'knowledge_id': item['id'], 'title': item['title'],
             'source_ref': item.get('source_ref'), 'score': item.get('score')}
            for item in matches
        ],
    }
