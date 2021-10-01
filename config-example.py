left_config = dict(
    side=LEFT,
    listen_port=1935,
    connect_from_to_port=[
        ("10.0.0.42", "remote.server.io", 1234),
        ("192.168.1.1", "remote.server.io", 1234),
    ],
)

right_config = dict(
    side=RIGHT,
    listen_port=1234,
    connect_from_to_port=[
        ("127.0.0.1", "127.0.0.1", 1935),
    ],
)
