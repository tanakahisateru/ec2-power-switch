#!/usr/bin/env python3

from collections import OrderedDict
import configparser
import os
import subprocess
import json
from datetime import datetime, timedelta, timezone
import threading
import time
import tkinter as tk
from tkinter import ttk
from attr import dataclass

@dataclass
class EC2InstanceConfig:
    id: str
    display_name: str
    user: str
    directory: str | None


@dataclass
class EC2InstanceStatus:
    config: EC2InstanceConfig
    id: str
    name: str
    state: str
    public_ip: str | None
    elapsed_time: timedelta | None


def get_ec2_instance_configs(ini_path) -> OrderedDict[str, EC2InstanceConfig]:
    if not os.path.exists(ini_path):
        raise FileNotFoundError(f"{ini_path} not found")

    ini = configparser.ConfigParser()
    instances = OrderedDict()
    with open(ini_path) as f:
        ini.read_file(f)
        for section_name in ini.sections():
            section = ini[section_name]
            id = section.get('id')
            if not id:
                continue
            instances[id] = EC2InstanceConfig(
                id = id,
                display_name = section_name,
                user = section.get('user', 'ec2-user'),
                directory = section.get('directory', None)
            )
    return instances


def get_ec2_instance_states(config: OrderedDict[str, EC2InstanceConfig]) -> OrderedDict[str, EC2InstanceStatus]:
    instance_ids = config.keys()
    command = [
        'aws', 'ec2', 'describe-instances',
        '--instance-ids', *instance_ids,
        '--query', "Reservations[*].Instances[*].[InstanceId, Tags[?Key=='Name'].Value | [0], State.Name, PublicIpAddress, LaunchTime]",
        '--output', 'json'
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(result.stderr)
    out = json.loads(result.stdout)

    instances = OrderedDict()
    current_time = datetime.now(timezone.utc)
    for reservation in out:
        for items in reservation:
            id = items[0]
            config_item = config[id]
            launch_time = datetime.strptime(items[4], '%Y-%m-%dT%H:%M:%S%z').replace(tzinfo=timezone.utc)
            state = items[2]
            elapsed_time = current_time - launch_time if state == 'running' else None
            instances[id] = EC2InstanceStatus(
                config = config_item,
                id = id,
                name = items[1] if items[1] else config_item.display_name,
                state = state,
                public_ip = items[3] if items[3] else None,
                elapsed_time = elapsed_time
            )
    return instances


def start_ec2_instance(instance: EC2InstanceStatus):
    command = [
        'aws', 'ec2', 'start-instances',
        '--instance-ids', instance.config.id
    ]
    subprocess.run(command)
    global status_watching_burst
    status_watching_burst = 10


def stop_ec2_instance(instance: EC2InstanceStatus):
    command = [
        'aws', 'ec2', 'stop-instances',
        '--instance-ids', instance.config.id
    ]
    subprocess.run(command)
    global status_watching_burst
    status_watching_burst = 10


def open_vscode_remote_ssh(instance: EC2InstanceStatus):
    user = instance.config.user
    host = instance.public_ip
    dir = instance.config.directory
    command = [
        'code', '--new-window', '--remote',
        f'ssh-remote+{instance.config.user}@{instance.public_ip}'
    ]
    if dir:
        command.append(dir)
    subprocess.run(command)


def format_elapsed_time(t: timedelta | None) -> str:
    if t is None:
        return ''
    hours, remainder = divmod(t.total_seconds(), 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{int(hours)}:{int(minutes):02}"


def update_treeview(config, states, tree, lock):
    new_states = get_ec2_instance_states(config)

    lock.acquire()
    states.clear()
    for item in tree.get_children():
        tree.delete(item)
    for instance in new_states.values():
        states[instance.id] = instance
        tree.insert("", "end", values=(
            instance.id,
            instance.name,
            instance.state,
            instance.public_ip if instance.public_ip else '',
            format_elapsed_time(instance.elapsed_time)
        ))
    lock.release()

continue_watching = True
status_watching_burst = 0

def status_watching_worker(config, states, tree, lock):
    update_treeview(config, states, tree, lock)
    global status_watching_burst
    tick = 0
    while continue_watching:
        interval = 60 if status_watching_burst <= 0 else 6
        tick += 1
        if tick % interval == 0:
            update_treeview(config, states, tree, lock)
            status_watching_burst -= 1 if status_watching_burst > 0 else 0
        time.sleep(1)


if __name__ == "__main__":
    root = tk.Tk()
    root.title("EC2 Power Switch")

    # 表を作成
    tree = ttk.Treeview(root)
    tree["columns"] = (1, 2, 3, 4, 5)
    tree["show"] = "headings"
    tree.heading(1, text="インスタンスID")
    tree.heading(2, text="名前")
    tree.heading(3, text="状態")
    tree.heading(4, text="IPアドレス")
    tree.heading(5, text="経過時間")
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    lock = threading.Lock()
    def selected_instance_state():
        lock.acquire()
        s = ec2_states[tree.item(tree.selection()[0], 'values')[0]]
        lock.release()
        return s

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="起動", command=lambda: start_ec2_instance(selected_instance_state()))
    menu.add_command(label="停止", command=lambda: stop_ec2_instance(selected_instance_state()))
    menu.add_command(label="VSCode Remote SSH", command=lambda: open_vscode_remote_ssh(selected_instance_state()))
    menu.add_separator()
    menu.add_command(label="更新", command=lambda: update_treeview(ec2_configs, ec2_states, tree))

    def show_menu(e):
        item = tree.identify_row(e.y)
        if item:
            tree.selection_set(item)
            s = selected_instance_state()
            if s.state == 'running':
                menu.entryconfig(0, state=tk.DISABLED)
                menu.entryconfig(1, state=tk.NORMAL)
                menu.entryconfig(2, state=tk.NORMAL if s.public_ip else tk.DISABLED)
            else:
                menu.entryconfig(0, state=tk.NORMAL)
                menu.entryconfig(1, state=tk.DISABLED)
                menu.entryconfig(2, state=tk.DISABLED)

            menu.post(e.x_root, e.y_root)

    tree.bind("<Button-2>", show_menu)

    ec2_configs = get_ec2_instance_configs('instances.ini')
    ec2_states = OrderedDict()

    # update_treeview(ec2_configs, ec2_states, tree, lock)
    thread = threading.Thread(target=status_watching_worker, args=(ec2_configs, ec2_states, tree, lock))
    thread.start()

    root.mainloop()

    continue_watching = False
    thread.join()
