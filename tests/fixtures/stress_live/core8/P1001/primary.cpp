#include <iostream>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    long long a, b;
    if (!(std::cin >> a >> b)) return 1;
    std::cout << a + b << '\n';
    return 0;
}
