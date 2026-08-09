"""直播切片：录像 → 转写 → 语义分段 → 商品对齐 → 双出口。

**双出口是这一段和市面上所有切片工具的分界线。**

FunClip、HotClip 这类工具的产物只有一样：短视频。而这个项目里，一场直播
应该产出两样东西——

* 视频切片 → 素材中心（``asset``）
* **主播的口播话术 → ``knowledge_item(biz_type=script)`` → RAG**

第二条才是它接进这套体系的理由。主播讲了两小时"这个面料起球吗、掉色吗、
薄不薄"，那是最真实的商品知识，而客服知识库现在全是通用问答。
切片工具不做这件事，因为它们没有知识库可以喂。

选型见 :mod:`.asr` 开头：站在 FunASR 上，不 vendor 现成的切片应用。
"""

from .transcript import Sentence, Transcript, build_hotwords

__all__ = ["Sentence", "Transcript", "build_hotwords"]
