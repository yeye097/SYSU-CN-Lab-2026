import socket
import threading

HOST = "172.20.10.3"
PORT = 55555

#创建监听用的套接字
#SOCK_STREAM表示TCP套接字，AF_INET表示IPv4
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
    server_socket.bind((HOST, PORT))
    server_socket.listen(2)

    clients = []
    try:
        for name in ("C1", "C2"):
            connection, address = server_socket.accept()
            clients.append(connection)
            print(f"{name} connected from {address}")

        def relay(source, target, sender_name):
            buffer = b""

            try:
                #会返回对端的IP地址和端口号，【0】是IP地址
                sender_ip = source.getpeername()[0]
                while True:
                    chunk = source.recv(4096)

                    if not chunk:
                        print(f"{sender_name} disconnected")
                        break
                    buffer += chunk

                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        prefix = f"{sender_name}({sender_ip}): "
                        message = prefix.encode("utf-8") + line + b"\n"

                        target.sendall(message)
                        print(f"Relayed from {sender_name}: {line.decode('utf-8')}")

            except OSError as error:
                print(f"{sender_name} connection error: {error}")

            finally:
                for connection in clients:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        c1_to_c2 = threading.Thread(target = relay, args=(clients[0],clients[1],"C1"))
        c2_to_c1 = threading.Thread(target = relay, args=(clients[1],clients[0],"C2"))

        #启动两个线程，主线程继续走下一行
        c1_to_c2.start()
        c2_to_c1.start()

        c1_to_c2.join()
        c2_to_c1.join()

    finally:
        for connection in clients:
            connection.close()