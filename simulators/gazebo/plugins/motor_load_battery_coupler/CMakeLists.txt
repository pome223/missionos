cmake_minimum_required(VERSION 3.16)
project(motor_load_battery_coupler)

# Build a gz-sim System plugin that couples multicopter motor load to a
# LinearBatteryPlugin power draw. This is built INSIDE the px4-gazebo container
# image (or against matching gz-sim dev packages), because the gz-sim ABI must
# match the runtime. See README.md for integration + boundary notes.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# GZ_VERSION selects the matching gz-sim major version. Override to match the
# container image, e.g. -DGZ_VERSION=harmonic  (gz-sim8) or garden (gz-sim7).
set(GZ_VERSION "harmonic" CACHE STRING "gz-sim distribution to build against")

if(GZ_VERSION STREQUAL "harmonic")
  set(GZ_SIM_PKG gz-sim8)
  set(GZ_PLUGIN_PKG gz-plugin2)
  set(GZ_TRANSPORT_PKG gz-transport13)
  set(GZ_COMMON_PKG gz-common5)
  set(GZ_MSGS_PKG gz-msgs10)
elseif(GZ_VERSION STREQUAL "garden")
  set(GZ_SIM_PKG gz-sim7)
  set(GZ_PLUGIN_PKG gz-plugin2)
  set(GZ_TRANSPORT_PKG gz-transport12)
  set(GZ_COMMON_PKG gz-common5)
  set(GZ_MSGS_PKG gz-msgs9)
else()
  message(FATAL_ERROR "Unsupported GZ_VERSION='${GZ_VERSION}' (use harmonic|garden)")
endif()

find_package(${GZ_SIM_PKG} REQUIRED)
find_package(${GZ_PLUGIN_PKG} REQUIRED COMPONENTS register)
find_package(${GZ_TRANSPORT_PKG} REQUIRED)
find_package(${GZ_COMMON_PKG} REQUIRED)
find_package(${GZ_MSGS_PKG} REQUIRED)

add_library(MotorLoadBatteryCoupler SHARED
  MotorLoadBatteryCoupler.cc
)

target_include_directories(MotorLoadBatteryCoupler PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

target_link_libraries(MotorLoadBatteryCoupler PRIVATE
  ${GZ_SIM_PKG}::core
  ${GZ_PLUGIN_PKG}::register
  ${GZ_TRANSPORT_PKG}::core
  ${GZ_COMMON_PKG}::core
  ${GZ_MSGS_PKG}::core
)

# Install next to the other gz-sim system plugins so it is discoverable on
# GZ_SIM_SYSTEM_PLUGIN_PATH.
install(TARGETS MotorLoadBatteryCoupler
  LIBRARY DESTINATION lib/gz-sim/plugins
)
