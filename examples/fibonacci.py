"""打印前 10 个斐波那契数（从 0、1 开始）。"""


def fibonacci(count: int) -> list[int]:
    """返回从 0、1 开始的前 count 个斐波那契数。"""
    numbers = []
    a, b = 0, 1
    for _ in range(count):
        numbers.append(a)
        a, b = b, a + b
    return numbers


if __name__ == "__main__":
    for value in fibonacci(10):
        print(value)
