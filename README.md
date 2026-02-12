Zat-Agent: Hybrid SDN Telemetry \& Orchestration Platform



Version: 2.2.0 (Prototype)

License: Apache 2.0



1\. Abstract



The Zat-Agent Platform is a custom Software-Defined Networking (SDN) reference architecture designed to modernize the observability and management of Linux-based edge infrastructure. By implementing a Hybrid Control Plane, the system addresses the latency and scalability limitations of traditional polling protocols (SNMP) while avoiding the implementation complexity of full enterprise NETCONF stacks.



This project serves as a practical implementation of the IETF "YANG Push" telemetry concepts, demonstrating how gRPC-based streaming and Intent-Based Networking (IBN) can be harmonized to create a responsive, low-overhead management system for resource-constrained edge devices and IoT gateways.



2\. Architectural Design



The system adheres to a strict Client-Server model with a separation of duties between the Orchestration Layer (Controller) and the Infrastructure Layer (Agent).



A. The Hybrid Control Plane



Unlike traditional architectures that rely on a single protocol for all operations, Zat-Agent decouples the control plane into two optimized channels:



Transactional Management Plane (HTTP/1.1):



Purpose: Configuration management and state synchronization.



Mechanism: RESTful API endpoints handling JSON-formatted intents.



Rationale: HTTP provides reliability and ubiquity for low-frequency "Write" operations (e.g., changing OSPF weights, assigning IP addresses), where transactional integrity is prioritized over raw speed.



Streaming Telemetry Plane (gRPC/HTTP2):



Purpose: Real-time observability and sensor data collection.



Mechanism: Server-side streaming RPCs using Protocol Buffers (Protobuf).



Rationale: gRPC offers significant performance advantages for high-frequency "Read" operations through binary serialization, header compression, and persistent TCP connections, enabling sub-second sampling rates essential for anomaly detection.



B. Dynamic Resource Provisioning



To optimize resource utilization on edge devices, the Agent operates in a default Passive State. Heavy telemetry processes (thread pools, sockets) are not pre-allocated.



Trigger: The Controller issues a specific provisioning intent via the Management Plane.



Action: The Agent's transaction manager dynamically instantiates the telemetry service logic ("Just-In-Time Provisioning").



Result: This "Zero-Touch" approach ensures that network resources are consumed only when active monitoring is required.



3\. Core Capabilities



Service Gateway \& Access Control



The Controller functions as a secure Service Gateway, abstracting the underlying network topology from external consumers (e.g., Data Analysts, Dashboards).



Proxy Architecture: External clients connect via secure WebSockets. The Controller bridges these connections to internal gRPC streams, performing protocol translation (Binary -> JSON) in real-time.



Authorization: A strict approval workflow ensures that no external entity can access infrastructure telemetry without explicit administrative grant.



Virtual Operational Datastore



The Agent implements a Virtual Datastore abstraction layer. Instead of maintaining a redundant state database (like sysrepo), the Agent maps standardized data models (XPaths) directly to system calls.



System Metrics: Maps paths like /system/cpu to Linux kernel counters (via procfs/psutil).



Hardware Sensors: Maps paths like /hardware/temp to physical sensors (I2C/GPIO) or simulation logic.



Advantage: This architecture reduces memory footprint by 90% compared to traditional YANG-based agents while maintaining API compatibility.



Automated Topology Discovery



The platform features a physics-based visualization engine that autonomously builds a live network map.



Discovery: Utilizing OSPF neighbor adjacencies and subnet analysis.



Flow Analysis: An integrated IPFIX (NetFlow v10) collector aggregates traffic flows, visualizing bandwidth utilization between nodes in real-time.



4\. Technical Implementation \& Workflow



The operational lifecycle follows a rigorous sequence to ensure security and stability.



Phase I: Discovery \& Registration



Initialization: The Agent initializes and verifies local interfaces.



Beaconing: A dedicated discovery thread emits periodic HTTP heartbeats containing device metadata (Hostname, Management IP).



Inventory: The Controller aggregates these heartbeats into a live inventory, making the device immediately available for orchestration.



Phase II: Capability Negotiation (JIT)



Probe: Upon operator request, the Controller sends a provisioning intent to the Agent via HTTP (Port 8000).



Service Start: The Agent spins up the gRPC listener thread on Port 50051.



Negotiation: The Controller establishes a transient connection to query the GetCapabilities RPC. The Agent scans its local hardware (e.g., detecting if it is a Raspberry Pi with GPIO sensors vs. a Virtual Router) and returns a list of supported telemetry paths.



Phase III: Persistent Streaming



Subscription: The Controller initiates a persistent HTTP/2 connection and subscribes to specific XPaths (e.g., /iot/sensor/voltage).



Transmission: The Agent enters a zero-overhead event loop, reading sensors and serializing data into compact Protobuf frames.



Relay: The Controller forwards this data to the visualization layer or external WebSocket clients with millisecond latency.



5\. Developer Guide: Extension Architecture



The modular design allows for rapid integration of new data sources (e.g., IoT sensors, Custom Applications).



Extending the Data Model



To introduce a new metric, developers must modify the GrpcPlugin (agent/plugins/grpc\_plugin.py).



1\. Register the Capability:

Update the GetCapabilities RPC to advertise the new path.



def GetCapabilities(self, request, context):

&nbsp;   sensors = \[

&nbsp;       "/system/cpu/utilization",

&nbsp;       "/custom/new\_metric\_path"  # Register new metric

&nbsp;   ]

&nbsp;   return zat\_pb2.CapabilitiesResponse(supported\_sensors=sensors, ...)





2\. Implement the Logic:

Add the collection logic within the streaming loop.



def Subscribe(self, request, context):

&nbsp;   # ... inside event loop ...

&nbsp;   if request.xpath == "/custom/new\_metric\_path":

&nbsp;       # Logic to read from file, API, or hardware

&nbsp;       val = my\_custom\_reading\_function()

&nbsp;   

&nbsp;   yield zat\_pb2.TelemetryData(..., value=val)





6\. Deployment \& Installation



Environment Requirements



Runtime: Python 3.10+



Compiler: protoc (Protocol Buffer Compiler)



Controller: Can run on Windows/Mac/Linux.



Agent: Designed for Debian-based Linux routers or Raspberry Pi OS (Raspbian).



Infrastructure Prerequisites (Agent Node)



Since the Agent manages network routing, the underlying Linux system must have the FRRouting (FRR) suite installed to support OSPF/BGP intent execution.



\# On the Agent/Router Node (Debian/Ubuntu):

sudo apt update

sudo apt install frr

\# Ensure the 'vtysh' command is accessible





1\. Compile Protocols



You must generate the Python gRPC bindings in both the Agent and Controller directories before running:



\# Run this in the root project folder

python -m grpc\_tools.protoc -I./proto --python\_out=./agent --grpc\_python\_out=./agent ./proto/zat.proto

python -m grpc\_tools.protoc -I./proto --python\_out=./controller --grpc\_python\_out=./controller ./proto/zat.proto





2\. Start the Controller



cd controller

pip install -r requirements.txt

python controller.py





Dashboard: http://localhost:5000



Client Gateway: http://localhost:5000/client



3\. Start the Agent



cd agent

pip install -r requirements.txt

python main.py





7\. License



This project is licensed under the Apache License 2.0. See the LICENSE file for details.

