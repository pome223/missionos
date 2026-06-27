// MotorLoadBatteryCoupler — gz-sim System that models a multicopter battery
// whose discharge is coupled to actual rotor effort, and publishes the result
// as a gz.msgs.BatteryState topic.
//
// Why this owns the battery integration instead of driving the stock
// LinearBatteryPlugin: gz-sim 8 (Harmonic) only exposes the battery's discharge
// as an on/off <power_draining_topic> — there is NO proportional power-set
// interface another system can call. To get a faithful, proportional motor-load
// coupling we integrate state-of-charge here from the rotor-derived electrical
// load and publish BatteryState on the same topic the probe already reads.
//
// TRUTH-SURFACE NOTE: this is a Gazebo SITL simulation model. It produces a
// physics-coupled *simulated* endurance signal, NOT real power-module endurance
// evidence. The probe reads the BatteryState topic as a separate observed
// signal and never overwrites the PX4 battery_status estimate.

#ifndef BOILED_CLAW_MOTOR_LOAD_BATTERY_COUPLER_HH_
#define BOILED_CLAW_MOTOR_LOAD_BATTERY_COUPLER_HH_

#include <chrono>
#include <memory>
#include <string>
#include <vector>

#include <gz/sim/System.hh>
#include <gz/sim/Entity.hh>
#include <gz/transport/Node.hh>

namespace boiled_claw
{
/// \brief Couples rotor angular velocity to a self-integrated battery state.
///
/// SDF parameters (all optional):
///   <battery_name>        name used in the published topic path.
///   <state_topic>         explicit BatteryState topic (overrides the default
///                         /model/<model>/battery/<battery_name>/state).
///   <rotor_joint>         repeated; joint names of the rotors to sample.
///   <idle_power_w>        avionics/baseline draw applied even at zero thrust.
///   <hover_power_w>       aggregate electrical power at nominal hover speed.
///   <hover_rotor_rad_s>   rotor angular speed (rad/s) used to normalize hover.
///                         Expressed as the TRUE motor speed (see
///                         <rotor_velocity_slowdown>).
///   <rotor_velocity_slowdown>
///                         gz's MulticopterMotorModel spins the *visual* rotor
///                         joint at (true_motor_speed / rotorVelocitySlowdownSim)
///                         — default 10 on the PX4 x500. The JointVelocity
///                         component therefore reads that slowed value, so we
///                         multiply it back by this factor to recover the true
///                         motor speed before applying the power model. Set to
///                         the same value as the model's rotorVelocitySlowdownSim
///                         (defaults to 1.0 = no compensation).
///   <max_power_w>         clamp so a runaway speed cannot zero the battery.
///   <capacity_ah>         pack capacity in amp-hours.
///   <initial_charge_ah>   starting charge (defaults to capacity).
///   <voltage_full_v>      open-circuit voltage at full charge.
///   <voltage_empty_v>     open-circuit voltage at empty.
///   <publish_rate_hz>     BatteryState publish rate.
///
/// Power model (per update):
///   omega_sum2 = sum_i (omega_i)^2
///   load_w = clamp(idle + (hover-idle) * omega_sum2/(n * hover_rad_s^2),
///                  idle, max)
/// SoC integration:
///   voltage = v_empty + (v_full - v_empty) * soc
///   current = load_w / voltage
///   charge_ah -= current * dt_hours
class MotorLoadBatteryCoupler
    : public gz::sim::System,
      public gz::sim::ISystemConfigure,
      public gz::sim::ISystemPreUpdate
{
 public:
  MotorLoadBatteryCoupler();
  ~MotorLoadBatteryCoupler() override;

  void Configure(const gz::sim::Entity &entity,
                 const std::shared_ptr<const sdf::Element> &sdf,
                 gz::sim::EntityComponentManager &ecm,
                 gz::sim::EventManager &eventMgr) override;

  void PreUpdate(const gz::sim::UpdateInfo &info,
                 gz::sim::EntityComponentManager &ecm) override;

 private:
  /// \brief Resolve rotor joint entities by name; logs any that don't resolve.
  void ResolveRotorJoints(gz::sim::EntityComponentManager &ecm);

  /// \brief Open-circuit voltage at the current state of charge.
  double VoltageAtSoc(double soc) const;

  /// \brief Publish the current BatteryState on the state topic.
  void PublishState(double loadW, double currentA);

  gz::sim::Entity model_{gz::sim::kNullEntity};
  std::string batteryName_{"linear_battery"};
  std::string stateTopic_;
  std::vector<std::string> rotorJointNames_;
  std::vector<gz::sim::Entity> rotorJoints_;
  bool jointsResolved_{false};

  // Load model.
  double idlePowerW_{12.0};
  double hoverPowerW_{180.0};
  double hoverRotorRadS_{700.0};
  double rotorVelocitySlowdown_{1.0};
  double maxPowerW_{600.0};

  // Battery model.
  double capacityAh_{5.2};
  double chargeAh_{5.2};
  double voltageFullV_{25.2};
  double voltageEmptyV_{21.0};

  // Publishing.
  double publishRateHz_{2.0};
  std::chrono::steady_clock::duration publishPeriod_{};
  std::chrono::steady_clock::time_point lastPublish_{};

  gz::transport::Node node_;
  gz::transport::Node::Publisher statePub_;
};
}  // namespace boiled_claw

#endif  // BOILED_CLAW_MOTOR_LOAD_BATTERY_COUPLER_HH_
