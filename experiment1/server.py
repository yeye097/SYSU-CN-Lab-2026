import socket
import threading

HOST = "127.0.0.1"
PORT = 55555

#创建监听用的套接字
#SOCK_STREAM表示TCP套接字，AF_INET表示IPv4
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
    #绑定服务器的本地IP地址与端口，作为欢迎socket
    server_socket.bind((HOST, PORT))
    #将套接字设置为监听状态；2是待接收连接队列的backlog参数
    server_socket.listen(2)

    #保存两个client的连接套接字
    clients = []
    try:
        for name in ("C1", "C2"):
            #accept()包含针对通信对象新创建的连接套接字和客户端的地址信息，返回一个元组
            connection, address = server_socket.accept()
            clients.append(connection)
            print(f"{name} connected from {address}")

        def relay(source, target, sender_name):
            buffer = b"" #字节缓冲区，保存当前尚未处理完的数据

            try:
                #会返回对端的IP地址和端口号，【0】是IP地址
                sender_ip = source.getpeername()[0]
                while True:
                    #每次最多读取4096字节；可能得到半条或多条消息
                    chunk = source.recv(4096)

                    #对方结束发送，且之前的chunk已经读完，recv返回b“”
                    if not chunk:
                        print(f"{sender_name} disconnected")
                        break
                    buffer += chunk
                    #根据换行符分割消息
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        #构造名字和IP前缀
                        prefix = f"{sender_name}({sender_ip}): "
                        #split去掉了换行符，这里补回换行符供接收方解析
                        message = prefix.encode("utf-8") + line + b"\n"

                        target.sendall(message)
                        print(f"Relayed from {sender_name}: {line.decode('utf-8')}")

            except OSError as error:
                #捕获接受、发送或套接字产生的网络相关异常
                print(f"{sender_name} connection error: {error}")

            finally:
                #当前会话结束，关闭两个客户端的连接套接字
                for connection in clients:
                    try:
                        # 同时停止读、写，使另一线程等待中的 recv() 能够结束
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        # 连接可能已经被停止，清理阶段继续处理其余连接
                        pass

        c1_to_c2 = threading.Thread(target = relay, args=(clients[0],clients[1],"C1"))
        c2_to_c1 = threading.Thread(target = relay, args=(clients[1],clients[0],"C2"))

        #启动两个线程，主线程继续走下一行
        c1_to_c2.start()
        c2_to_c1.start()
        #主线程等待两个转发线程结束
        c1_to_c2.join()
        c2_to_c1.join()

    finally:
        for connection in clients:
            #等两个线程的join()返回后，释放套接字资源
            connection.close()