# Output Format

Use this reference when summarizing each split novel part. Write concise but information-dense Markdown.

## Context Rule

Before writing `总结N`, read all earlier summaries for the same article from `<文章名>/输出总结` and treat them as prior context. Do not rely only on the immediately previous part.

## Tag Rule

Create one label for each part:

- Prefer 4-12 Chinese characters.
- Capture the dominant story movement, not a generic theme.
- Avoid empty labels such as `剧情发展`, `人物成长`, `故事推进`.
- Avoid filename-risk characters: `< > : " / \ | ? *`.

Examples of stronger labels: `边城疑云`, `宗门暗潮`, `身世裂变`, `旧怨复燃`, `帝都棋局`.

## Summary Template

```markdown
# 《文章名》-总结001-主要内容标签

- 标签：主要内容标签
- 对应拆分：`《文章名》-拆分001-主要内容标签.md`
- 处理范围：章节/段落范围；估算字数
- 前情上下文：说明本次总结参考了哪些先前总结；第一段写“无”

## 核心摘要

用 600-1200 字概括本部分主线，保持因果清晰：谁做了什么，为什么，造成什么后果，留下什么问题。

## 故事大纲

按时间线列出关键事件、转折、高潮和影响。若多条人物线并行，按人物或地点分组。

## 角色

记录主角、配角、印象深刻的次要人物。关注标签、成长历程、思想发展、能力变化、年龄/身材/样貌、标志性特征、核心动机、主要冲突和说话习惯。

## 冲突

区分贯穿全书的核心冲突和本部分的次要冲突。说明冲突如何被体现、升级、遮掩或暂时解决。

## 核心悬念

记录被隐藏、部分揭露或反转的信息。包括物件、意象、能力、身份、真相、误导和更大悬念。

## 背景/世界观

记录时代背景、地点、世界规则、势力结构、关键设定和时间线记忆。玄幻、科幻等类型要写清设定如何影响剧情。

## 叙事视角

说明叙事人称和视角范围：第一人称、第三人称有限、第三人称全知或混合视角。记录视角切换对悬念和信息差的影响。

## 节奏控制

分析动作、内心独白、场景描述、对话密度、段落长短和紧张/缓和节奏。指出本部分节奏最快和最慢的位置。

## 伏笔追踪

列出仍需后文验证的伏笔、未解问题和可疑细节。
```

## Fallback Analysis Checklist

Use this checklist when related skills such as `$character-analysis`, `$conflict-analysis`, `$core-suspense-analysis`, `$worldbuilding-analysis`, `$story-outline-analysis`, `$narrative-perspective-analysis`, or `$pacing-control-analysis` are not available.

- 角色：人物的欲望、恐惧、能力边界、关系变化和可被读者记住的特征。
- 冲突：表层目标冲突、价值观冲突、资源冲突、身份冲突、长期核心矛盾。
- 悬念：读者已知、角色未知；角色已知、读者未知；双方都未知但被反复暗示。
- 背景：规则、限制、代价、历史、地理、势力、技术/修行/制度设定。
- 大纲：事件顺序、因果链、关键转折、高潮、后果。
- 视角：信息差、可信度、镜头距离、叙述者是否隐瞒。
- 节奏：动作短促、心理铺陈、描述停顿、对话推进、章节钩子。
