#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

struct Node {
    int id, size = 1;
    std::uint32_t priority;
    Node *left = nullptr, *right = nullptr, *parent = nullptr;
};

int size(Node* node) { return node ? node->size : 0; }
void pull(Node* node) {
    if (!node) return;
    node->size = 1 + size(node->left) + size(node->right);
    if (node->left) node->left->parent = node;
    if (node->right) node->right->parent = node;
}
Node* merge(Node* left, Node* right) {
    if (!left) { if (right) right->parent = nullptr; return right; }
    if (!right) { left->parent = nullptr; return left; }
    if (left->priority < right->priority) {
        left->right = merge(left->right, right);
        pull(left); left->parent = nullptr; return left;
    }
    right->left = merge(left, right->left);
    pull(right); right->parent = nullptr; return right;
}
void split(Node* root, int count, Node*& left, Node*& right) {
    if (!root) { left = right = nullptr; return; }
    if (size(root->left) >= count) {
        split(root->left, count, left, root->left);
        right = root; pull(right); right->parent = nullptr;
    } else {
        split(root->right, count - size(root->left) - 1, root->right, right);
        left = root; pull(left); left->parent = nullptr;
    }
}
int position(Node* node) {
    int result = size(node->left);
    while (node->parent) {
        if (node == node->parent->right) result += size(node->parent->left) + 1;
        node = node->parent;
    }
    return result;
}
Node* kth(Node* root, int index) {
    while (root) {
        int left = size(root->left);
        if (index < left) root = root->left;
        else if (index == left) return root;
        else { index -= left + 1; root = root->right; }
    }
    return nullptr;
}

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    int n, operations;
    if (!(std::cin >> n >> operations)) return 1;
    std::vector<Node> nodes(n + 1);
    std::vector<Node*> by_id(n + 1);
    std::uint32_t state = 0x9e3779b9U;
    auto random_priority = [&]() {
        state ^= state << 13; state ^= state >> 17; state ^= state << 5; return state;
    };
    Node* root = nullptr;
    for (int i = 0, id; i < n; ++i) {
        std::cin >> id;
        nodes[id].id = id; nodes[id].priority = random_priority(); by_id[id] = &nodes[id];
        root = merge(root, &nodes[id]);
    }
    auto move_to = [&](Node* node, int target) {
        int current = position(node);
        Node *before, *rest, *middle, *after;
        split(root, current, before, rest);
        split(rest, 1, middle, after);
        root = merge(before, after);
        split(root, target, before, after);
        root = merge(merge(before, middle), after);
    };
    while (operations--) {
        std::string operation;
        int value;
        std::cin >> operation >> value;
        if (operation == "Query") std::cout << kth(root, value - 1)->id << '\n';
        else if (operation == "Ask") std::cout << position(by_id[value]) << '\n';
        else if (operation == "Top") move_to(by_id[value], 0);
        else if (operation == "Bottom") move_to(by_id[value], n - 1);
        else {
            int delta; std::cin >> delta;
            if (delta) move_to(by_id[value], position(by_id[value]) + delta);
        }
    }
    return 0;
}
