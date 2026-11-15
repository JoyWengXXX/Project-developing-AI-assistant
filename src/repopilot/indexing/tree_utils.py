"""tree-sitter 抽取器共用的語法錯誤位置。"""

from tree_sitter import Node

MAX_ERROR_COUNT = 1000


def find_errors(root: Node) -> tuple[int, tuple[int, int] | None]:
    """最上層錯誤節點數與第一個錯誤的 (行, 欄)；只進入 `has_error` 的子樹，不遞迴。"""
    count = 0
    first: tuple[int, int] | None = None
    stack = [root]
    while stack and count < MAX_ERROR_COUNT:
        node = stack.pop()
        if node.is_error or node.is_missing:
            count += 1
            point = (node.start_point.row + 1, node.start_point.column)
            if first is None or point < first:
                first = point
        elif node.has_error:
            stack.extend(node.children)
    if count == 0:
        return 1, (root.start_point.row + 1, root.start_point.column)
    return count, first
