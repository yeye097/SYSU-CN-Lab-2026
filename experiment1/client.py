import socket
import threading
import sys

def receive_messages(sock):
    buffer = ""
    while True:
        try:
            data = sock.recv(1024)
            if not data:
                print("\n[系统] 服务端已断开连接。")
                break
            buffer += data.decode('utf-8')
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                if line:
                    sys.stdout.write(f"\r[对方发来消息] {line}\n")
                    sys.stdout.write("输入你的消息: ")
                    sys.stdout.flush()
        except Exception as e:
            print(f"\n[系统] 接收消息出错: {e}")
            break

def main():
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # 服务端 IP / 端口
    server_address = ('127.0.0.1', 55555)

    try:
        client_socket.connect(server_address)
        my_ip = client_socket.getsockname()[0]
        print(f"成功连接到服务端。本机IP: {my_ip}")
    except ConnectionRefusedError:
        print("连接失败，请确认服务端软件是否已经启动。")
        return

    recv_thread = threading.Thread(target=receive_messages, args=(client_socket,))
    recv_thread.daemon = True
    recv_thread.start()

    try:
        while True:
            msg = input("输入你的消息: ")
            if msg.lower() == 'quit':
                break
            if msg:
                # 只发正文 + \n；前缀（身份+IP）由服务端统一添加
                client_socket.sendall((msg + '\n').encode('utf-8'))
    except KeyboardInterrupt:
        print("\n[系统] 强制退出。")
    finally:
        try:
            # 先结束发送方向，接收方向暂时保留
            client_socket.shutdown(socket.SHUT_WR)
        except OSError as error:
            print(f"\n[系统] 结束发送时出现错误: {error}")

        # 等接收线程读到服务端结束发送，退出 receive_messages()
        recv_thread.join()

        # 接收线程结束后，再释放 socket
        client_socket.close()

if __name__ == "__main__":
    main()