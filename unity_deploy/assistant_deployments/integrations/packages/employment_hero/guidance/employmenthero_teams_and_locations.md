# Teams and Locations

Locations are the geographic layer of the workforce graph (offices,
sites, properties, depots — depending on the customer's vertical).
Teams group employees by function (Engineering, Customer Support,
Maintenance, etc.).

The synced `EmploymentHero/Locations` carries `name`, `address_line_1/2`,
`city`, `postcode`, `country`.  Each team has a `manager_id` and an
optional `location_id`.  `EmploymentHero/Teams/Memberships` is the
`(team_id, employee_id)` junction table.

## Common cross-record questions

```sql
-- Active employees at a location
SELECT e.first_name, e.last_name, e.position, t.name AS team
FROM   EmploymentHero/Employees e
JOIN   EmploymentHero/Teams t ON e.team_id = t.id
WHERE  e.location_id = '<location-id>'
   AND e.status = 'active';
```

Use `dm.filter_join(...)` to express this against the synced copy.
