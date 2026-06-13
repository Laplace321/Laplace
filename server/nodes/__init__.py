"""图节点（Pipeline B/C/A）。

每个节点 = ``async def node(state: PipelineState) -> PipelineState``；
节点之间通过 PipelineState 共享数据。
"""
