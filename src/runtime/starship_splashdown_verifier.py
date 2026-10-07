"""Check saved offshore water-entry geometry independently of its producer.

Run the common six-DOF trajectory/contact verifier before this check. A passing
record may still have a failed water-entry envelope. No wave/survival claim.
"""
import hashlib
import json
import math


def verify_splashdown(booster, goal):
    issues = []
    computed = False
    try:
        record = booster["splashdown"]
        if record["goal"] != goal or record["water_response_verified"] is not False or record["physical_execution"] is not False:
            raise ValueError("splashdown_goal_binding")
        site = booster["active_return_site"]
        site_hash = hashlib.sha256(json.dumps(site,sort_keys=True,separators=(",", ":"),allow_nan=False).encode()).hexdigest()
        if site_hash != goal["reference_site_sha256"] or site["site_id"] != "divert" or site["elevation_m"] != 0:
            raise ValueError("splashdown_reference_binding")
        receipt = booster.get("contact")
        if receipt is None:
            if record["contact_observed"] is not False:
                raise ValueError("splashdown_false_contact")
        else:
            p = receipt["point_eci_m"]
            v = receipt["point_velocity_eci_mps"]
            omega = 7.292115e-5
            relative = [v[0]+omega*p[1],v[1]-omega*p[0],v[2]]
            a, b = 6378137., 6378137.*(1-1/298.257223563)
            normal = [p[0]/a**2,p[1]/a**2,p[2]/b**2]
            length = math.sqrt(sum(x*x for x in normal))
            normal = [x/length for x in normal]
            speed = math.sqrt(sum(x*x for x in relative))
            vertical = sum(x*y for x,y in zip(relative,normal))
            horizontal = math.sqrt(max(0.,speed*speed-vertical*vertical))
            final = booster["final_state"]
            position = final["r_eci_m"]
            lat, lon = math.radians(site["latitude_deg"]), math.radians(site["longitude_deg"])+omega*final["time_s"]
            e2 = 1-b*b/(a*a)
            prime = a/math.sqrt(1-e2*math.sin(lat)**2)
            target = [prime*math.cos(lat)*math.cos(lon),prime*math.cos(lat)*math.sin(lon),prime*(1-e2)*math.sin(lat)]
            def unit(x):
                n=math.sqrt(sum(y*y for y in x))
                return [y/n for y in x]
            radial, toward = unit(position), unit(target)
            cosine = max(-1.,min(1.,sum(x*y for x,y in zip(radial,toward))))
            distance = a*math.acos(cosine)
            # Ellipsoid-normal up (geocentric radial differs at this latitude).
            up = unit([position[0]/a**2,position[1]/a**2,position[2]/b**2])
            q = final["q_body_to_eci"]
            body_z = [2*(q[1]*q[3]+q[0]*q[2]),2*(q[2]*q[3]-q[0]*q[1]),1-2*(q[1]**2+q[2]**2)]
            tilt = math.degrees(math.acos(max(-1.,min(1.,sum(x*y for x,y in zip(up,body_z))))))
            rate = math.sqrt(sum(x*x for x in final["omega_body_rad_s"]))
            checks = {"area":distance <= goal["area_radius_m"], "speed":speed <= goal["maximum_contact_speed_mps"],
                "vertical":-goal["maximum_downward_speed_mps"] <= vertical <= 0,
                "horizontal":horizontal <= goal["maximum_horizontal_speed_mps"],
                "tilt":tilt <= goal["maximum_tilt_deg"], "rate":rate <= goal["maximum_body_rate_rad_s"],
                "reserve":final["propellant_kg"] >= goal["minimum_propellant_kg"],
                "no_overlap":receipt["initial_overlap"] is False}
            if record["contact_observed"] is not True or record["checks"] != checks:
                raise ValueError("splashdown_check_mismatch")
            computed = all(checks.values())
        if record["controlled_water_entry_envelope_met"] is not computed:
            raise ValueError("splashdown_outcome_mismatch")
    except (ValueError,KeyError,TypeError,IndexError,ZeroDivisionError) as exc:
        issues.append({"code":str(exc)})
    return {"passed":not issues,"issues":issues,"controlled_water_entry_envelope_met":computed,
            "water_response_verified":False,"physical_execution":False}
