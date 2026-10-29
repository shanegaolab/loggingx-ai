from loggingx_ai.stacktrace import normalize, parse

JAVA = """2026-10-29 10:00:00 ERROR [http-nio-8080-exec-1] Request failed
java.lang.NullPointerException: Cannot invoke "User.getName()" because "user" is null
\tat com.shop.order.OrderService.submit(OrderService.java:88)
\tat org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:897)
\tat com.shop.web.OrderController.create(OrderController.java:41)
"""

PYTHON = """Traceback (most recent call last):
  File "/usr/lib/python3/site-packages/flask/app.py", line 1, in wsgi
  File "/app/shop/orders.py", line 12, in submit
    total = price / qty
ZeroDivisionError: division by zero
"""

GO = """panic: runtime error: index out of range
goroutine 1 [running]:
main.handler()
\t/app/orders/service.go:42 +0x1d
"""


def test_java_frames_and_location():
    parsed = parse(JAVA)
    assert parsed.error_type == "java.lang.NullPointerException"
    assert [frame.library for frame in parsed.frames] == [False, True, False]
    location = parsed.location()
    assert location.class_name == "com.shop.order.OrderService"
    assert location.probable_path() == "com/shop/order/OrderService.java"
    assert location.label() == "com.shop.order.OrderService.submit(OrderService.java:88)"


def test_fingerprint_ignores_line_numbers():
    assert parse(JAVA).fingerprint() == parse(JAVA.replace(":88)", ":90)")).fingerprint()


def test_python_innermost_frame_first():
    parsed = parse(PYTHON)
    assert parsed.error_type == "ZeroDivisionError"
    assert parsed.location().file == "/app/shop/orders.py"
    assert parsed.frames[-1].library


def test_go_panic():
    parsed = parse(GO)
    assert parsed.error_type == "panic"
    assert parsed.location().line == 42


def test_normalize_removes_varying_values():
    assert normalize('2026-10-29 10:00:01 order 12345 "abc" failed') == "order # * failed"
